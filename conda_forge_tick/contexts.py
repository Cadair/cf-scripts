import copy
import os
import typing
from dataclasses import dataclass

from networkx import DiGraph

from conda_forge_tick.lazy_json_backends import load

if typing.TYPE_CHECKING:
    from conda_forge_tick.migrators import Migrator
    from conda_forge_tick.migrators_types import AttrsTypedDict


if os.path.exists("all_feedstocks.json"):
    with open("all_feedstocks.json") as f:
        DEFAULT_BRANCHES = load(f).get("default_branches", {})
else:
    DEFAULT_BRANCHES = {}


@dataclass
class MigratorSessionContext:
    """Singleton session context. There should generally only be one of these"""

    graph: DiGraph = None
    smithy_version: str = ""
    pinning_version: str = ""
    dry_run: bool = True


@dataclass
class MigratorContext:
    """The context for a given migrator.
    This houses the runtime information that a migrator needs
    """

    session: MigratorSessionContext
    migrator: "Migrator"
    _effective_graph: DiGraph = None

    @property
    def effective_graph(self) -> DiGraph:
        if self._effective_graph is None:
            gx2 = copy.deepcopy(getattr(self.migrator, "graph", self.session.graph))

            # Prune graph to only things that need builds right now
            for node in list(gx2.nodes):
                if node not in self.session.graph.nodes:
                    continue

                with self.session.graph.nodes[node]["payload"] as _attrs:
                    attrs = copy.deepcopy(_attrs.data)
                base_branches = self.migrator.get_possible_feedstock_branches(attrs)
                filters = []
                for base_branch in base_branches:
                    attrs["branch"] = base_branch
                    filters.append(self.migrator.filter(attrs))

                if filters and all(filters):
                    gx2.remove_node(node)

            self._effective_graph = gx2

        return self._effective_graph


@dataclass
class FeedstockContext:
    package_name: str
    feedstock_name: str
    attrs: "AttrsTypedDict"
    _default_branch: str = None

    @property
    def default_branch(self):
        if self._default_branch is None:
            return DEFAULT_BRANCHES.get(f"{self.feedstock_name}", "main")
        else:
            return self._default_branch

    @default_branch.setter
    def default_branch(self, v):
        self._default_branch = v
