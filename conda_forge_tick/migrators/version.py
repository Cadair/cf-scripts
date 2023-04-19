import os
import typing
import functools
import random
from typing import (
    Sequence,
    Any,
    List,
)
import warnings
import logging

import networkx as nx
import conda.exceptions
from conda.models.version import VersionOrder

from conda_forge_tick.migrators.core import Migrator
from conda_forge_tick.contexts import FeedstockContext
from conda_forge_tick.utils import pushd
from conda_forge_tick.utils import sanitize_string
from conda_forge_tick.update_deps import get_dep_updates_and_hints
from conda_forge_tick.update_recipe import update_version

if typing.TYPE_CHECKING:
    from conda_forge_tick.migrators_types import (
        MigrationUidTypedDict,
        AttrsTypedDict,
        PackageName,
    )

SKIP_DEPS_NODES = [
    "ansible",
]

logger = logging.getLogger("conda_forge_tick.migrators.version")


def _fmt_error_message(errors, version):
    msg = (
        "The recipe did not change in the version migration, a URL did "
        "not hash, or there is jinja2 syntax the bot cannot handle!\n\n"
        "Please check the URLs in your recipe with version '%s' to make sure "
        "they exist!\n\n" % version
    )
    if len(errors) > 0:
        msg += "We also found the following errors:\n\n - %s" % (
            "\n - ".join(e for e in errors)
        )
        msg += "\n"
    return sanitize_string(msg)


class Version(Migrator):
    """Migrator for version bumping of packages"""

    max_num_prs = 3
    migrator_version = 0
    rerender = True
    name = "Version"

    def __init__(self, python_nodes, *args, **kwargs):
        self.python_nodes = python_nodes
        if "check_solvable" in kwargs:
            kwargs.pop("check_solvable")
        super().__init__(*args, **kwargs, check_solvable=False)

    def filter(self, attrs: "AttrsTypedDict", not_bad_str_start: str = "") -> bool:
        # if no new version do nothing
        vpri = attrs.get("version_pr_info", {})
        if "new_version" not in vpri or not vpri["new_version"]:
            return True

        # if no jinja2 version, then move on
        if "raw_meta_yaml" in attrs and "{% set version" not in attrs["raw_meta_yaml"]:
            return True

        conditional = super().filter(attrs)

        result = bool(
            conditional  # if archived/finished
            or len(
                [
                    k
                    for k in attrs.get("pr_info", {}).get("PRed", [])
                    if k["data"].get("migrator_name") == "Version"
                    # The PR is the actual PR itself
                    and k.get("PR", {}).get("state", None) == "open"
                ],
            )
            > self.max_num_prs
            or not vpri.get("new_version"),  # if no new version
        )

        try:
            version_filter = (
                # if new version is less than current version
                (
                    VersionOrder(str(vpri["new_version"]).replace("-", "."))
                    <= VersionOrder(
                        str(attrs.get("version", "0.0.0")).replace("-", "."),
                    )
                )
                # if PRed version is greater than newest version
                or any(
                    VersionOrder(self._extract_version_from_muid(h).replace("-", "."))
                    >= VersionOrder(str(vpri["new_version"]).replace("-", "."))
                    for h in attrs.get("pr_info", {}).get("PRed", set())
                )
            )
        except conda.exceptions.InvalidVersionSpec as e:
            name = attrs.get("name", "")
            warnings.warn(
                f"Failed to filter to to invalid version for {name}\nException: {e}",
            )
            version_filter = True

        skip_filter = False
        random_fraction_to_keep = (
            attrs.get("conda-forge.yml", {})
            .get("bot", {})
            .get("version_updates", {})
            .get("random_fraction_to_keep", None)
        )
        if random_fraction_to_keep is not None:
            curr_state = random.getstate()
            try:
                frac = float(random_fraction_to_keep)

                # the seeding here makes the filter stable given the current version
                # if there is no version in the recipe, we always accept
                # the version update
                # this rule avoids a weird edge case possibly of never
                # shipping a version if we always seed with 0.0.0
                if "version" not in attrs:
                    urand = 0.0
                else:
                    random.seed(a=str(attrs.get("version", "0.0.0")).replace("-", "."))
                    urand = random.uniform(0, 1)

                if urand <= frac:
                    skip_filter = True
            finally:
                random.setstate(curr_state)

        return result or version_filter or skip_filter

    def migrate(
        self,
        recipe_dir: str,
        attrs: "AttrsTypedDict",
        hash_type: str = "sha256",
        **kwargs: Any,
    ) -> "MigrationUidTypedDict":
        version = attrs.get("version_pr_info", {})["new_version"]

        # record the attempt
        with attrs["version_pr_info"] as vpri:
            if "new_version_attempts" not in vpri:
                vpri["new_version_attempts"] = {}
            if "new_version_errors" not in vpri:
                vpri["new_version_errors"] = {}
            if version not in vpri["new_version_attempts"]:
                vpri["new_version_attempts"][version] = 0
            vpri["new_version_attempts"][version] += 1

        with open(os.path.join(recipe_dir, "meta.yaml")) as fp:
            raw_meta_yaml = fp.read()

        updated_meta_yaml, errors = update_version(
            raw_meta_yaml,
            version,
            hash_type=hash_type,
        )

        if len(errors) == 0 and updated_meta_yaml is not None:
            with pushd(recipe_dir):
                with open("meta.yaml", "w") as fp:
                    fp.write(updated_meta_yaml)
                self.set_build_number("meta.yaml")

            return super().migrate(recipe_dir, attrs)
        else:
            with attrs["version_pr_info"] as vpri:
                vpri["new_version_errors"][version] = _fmt_error_message(
                    errors,
                    version,
                )
            return {}

    def pr_body(self, feedstock_ctx: FeedstockContext) -> str:
        pred = [
            (
                name,
                self.ctx.effective_graph.nodes[name]["payload"]["version_pr_info"][
                    "new_version"
                ],
            )
            for name in list(
                self.ctx.effective_graph.predecessors(feedstock_ctx.package_name),
            )
        ]
        body = ""

        # TODO: note that the closing logic needs to be modified when we
        #  issue PRs into other branches for backports
        open_version_prs = [
            muid["PR"]
            for muid in feedstock_ctx.attrs.get("pr_info", {}).get("PRed", [])
            if muid["data"].get("migrator_name") == "Version"
            # The PR is the actual PR itself
            and muid.get("PR", {}).get("state", None) == "open"
        ]

        # Display the url so that the maintainer can quickly click on it
        # in the PR body.
        about = feedstock_ctx.attrs.get("meta_yaml", {}).get("about", {})
        upstream_url = about.get("dev_url", "") or about.get("home", "")
        if upstream_url:
            upstream_url_link = ": see [upstream]({upstream_url})".format(
                upstream_url=upstream_url,
            )
        else:
            upstream_url_link = ""

        muid: dict
        body += (
            "It is very likely that the current package version for this "
            "feedstock is out of date.\n"
            "\n"
            "Checklist before merging this PR:\n"
            "- [ ] Dependencies have been updated if changed{upstream_url_link}\n"
            "- [ ] Tests have passed \n"
            "- [ ] Updated license if changed and `license_file` is packaged \n"
            "\n"
            "Information about this PR:\n"
            "1. Feel free to push to the bot's branch to update this PR if needed.\n"
            "2. The bot will almost always only open one PR per version.\n"
            "3. The bot will stop issuing PRs if more than {max_num_prs} "
            "version bump PRs "
            "generated by the bot are open. If you don't want to package a particular "
            "version please close the PR.\n"
            "4. If you want these PRs to be merged automatically, make an issue "
            "with <code>@conda-forge-admin,</code>`please add bot automerge` in the "
            "title and merge the resulting PR. This command will add our bot "
            "automerge feature to your feedstock.\n"
            "5. If this PR was opened in error or needs to be updated please add "
            "the `bot-rerun` label to this PR. The bot will close this PR and "
            "schedule another one. If you do not have permissions to add this "
            "label, you can use the phrase "
            "<code>@<space/>conda-forge-admin, please rerun bot</code> "
            "in a PR comment to have the `conda-forge-admin` add it for you.\n"
            "\n"
            "{closes}".format(
                upstream_url_link=upstream_url_link,
                max_num_prs=self.max_num_prs,
                closes="\n".join(
                    [f"Closes: #{muid['number']}" for muid in open_version_prs],
                ),
            )
        )
        # Statement here
        template = (
            "|{name}|{new_version}|[![Anaconda-Server Badge]"
            "(https://img.shields.io/conda/vn/conda-forge/{name}.svg)]"
            "(https://anaconda.org/conda-forge/{name})|\n"
        )
        if len(pred) > 0:
            body += "\n\nPending Dependency Version Updates\n--------------------\n\n"
            body += (
                "Here is a list of all the pending dependency version updates "
                "for this repo. Please double check all dependencies before "
                "merging.\n\n"
            )
            # Only add the header row if we have content.
            # Otherwise the rendered table in the github comment
            # is empty which is confusing
            body += (
                "| Name | Upstream Version | Current Version |\n"
                "|:----:|:----------------:|:---------------:|\n"
            )
        for p in pred:
            body += template.format(name=p[0], new_version=p[1])

        body += self._hint_and_maybe_update_deps(feedstock_ctx)

        return super().pr_body(feedstock_ctx, add_label_text=False).format(body)

    def _hint_and_maybe_update_deps(self, feedstock_ctx):
        update_deps = (
            feedstock_ctx.attrs.get("conda-forge.yml", {})
            .get("bot", {})
            .get("inspection", "hint")
        )
        logger.info("bot.inspection: %s", update_deps)
        if not update_deps:
            return ""
        else:
            if feedstock_ctx.attrs["feedstock_name"] in SKIP_DEPS_NODES:
                logger.info("Skipping dep update since node %s in rejectlist!")
                hint = "\n\nDependency Analysis\n--------------------\n\n"
                hint += (
                    "We couldn't run dependency analysis since this feedstock is "
                    "in the reject list for dep updates due to bot stability "
                    "issues!"
                )
            else:
                try:
                    _, hint = get_dep_updates_and_hints(
                        update_deps,
                        os.path.join(feedstock_ctx.feedstock_dir, "recipe"),
                        feedstock_ctx.attrs,
                        self.python_nodes,
                        "new_version",
                    )
                except Exception:
                    hint = "\n\nDependency Analysis\n--------------------\n\n"
                    hint += (
                        "We couldn't run dependency analysis due to an internal "
                        "error in the bot. :/ Help is very welcome!"
                    )

            return hint

    def commit_message(self, feedstock_ctx: FeedstockContext) -> str:
        assert isinstance(feedstock_ctx.attrs["version_pr_info"]["new_version"], str)
        return "updated v" + feedstock_ctx.attrs["version_pr_info"]["new_version"]

    def pr_title(self, feedstock_ctx: FeedstockContext) -> str:
        assert isinstance(feedstock_ctx.attrs["version_pr_info"]["new_version"], str)
        # TODO: turn False to True when we default to automerge
        if feedstock_ctx.attrs.get("conda-forge.yml", {}).get("bot", {}).get(
            "automerge",
            False,
        ) in {"version", True}:
            add_slug = "[bot-automerge] "
        else:
            add_slug = ""

        return (
            add_slug
            + feedstock_ctx.package_name
            + " v"
            + feedstock_ctx.attrs["version_pr_info"]["new_version"]
        )

    def remote_branch(self, feedstock_ctx: FeedstockContext) -> str:
        assert isinstance(feedstock_ctx.attrs["version_pr_info"]["new_version"], str)
        return feedstock_ctx.attrs["version_pr_info"]["new_version"]

    def migrator_uid(self, attrs: "AttrsTypedDict") -> "MigrationUidTypedDict":
        n = super().migrator_uid(attrs)
        assert isinstance(attrs["version_pr_info"]["new_version"], str)
        n["version"] = attrs["version_pr_info"]["new_version"]
        return n

    def _extract_version_from_muid(self, h: dict) -> str:
        return h.get("version", "0.0.0")

    @classmethod
    def new_build_number(cls, old_build_number: int) -> int:
        return 0

    def order(
        self,
        graph: nx.DiGraph,
        total_graph: nx.DiGraph,
    ) -> Sequence["PackageName"]:
        @functools.lru_cache(maxsize=1024)
        def _has_solver_checks(node):
            with graph.nodes[node]["payload"] as attrs:
                return (
                    attrs["conda-forge.yml"]
                    .get("bot", {})
                    .get(
                        "check_solvable",
                        False,
                    )
                )

        @functools.lru_cache(maxsize=1024)
        def _get_attemps_nr(node):
            with graph.nodes[node]["payload"] as attrs:
                with attrs["version_pr_info"] as vpri:
                    new_version = vpri.get("new_version", "")
                    attempts = vpri.get("new_version_attempts", {}).get(new_version, 0)
            return min(attempts, 3)

        def _get_attemps_r(node, seen):
            seen |= {node}
            attempts = _get_attemps_nr(node)
            for d in nx.descendants(graph, node):
                if d not in seen:
                    attempts = max(attempts, _get_attemps_r(d, seen))
            return attempts

        @functools.lru_cache(maxsize=1024)
        def _get_attemps(node):
            if _has_solver_checks(node):
                seen = set()
                return _get_attemps_r(node, seen)
            else:
                return _get_attemps_nr(node)

        def _desc_cmp(node1, node2):
            if _has_solver_checks(node1) and _has_solver_checks(node2):
                if node1 in nx.descendants(graph, node2):
                    return 1
                elif node2 in nx.descendants(graph, node1):
                    return -1
                else:
                    return 0
            else:
                return 0

        random.seed()
        nodes_to_sort = list(graph.nodes)
        return sorted(
            sorted(
                sorted(nodes_to_sort, key=lambda x: random.uniform(0, 1)),
                key=_get_attemps,
            ),
            key=functools.cmp_to_key(_desc_cmp),
        )

    def get_possible_feedstock_branches(self, attrs: "AttrsTypedDict") -> List[str]:
        """Return the valid possible branches to which to apply this migration to
        for the given attrs.

        Parameters
        ----------
        attrs : dict
            The node attributes

        Returns
        -------
        branches : list of str
            List if valid branches for this migration.
        """
        # make sure this is always a string
        return [str(attrs.get("branch", "main"))]
