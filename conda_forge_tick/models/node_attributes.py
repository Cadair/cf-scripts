from collections import defaultdict
from typing import Any, Literal, Self

from pydantic import (
    AnyUrl,
    BaseModel,
    Field,
    TypeAdapter,
    model_serializer,
    model_validator,
)
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from conda_forge_tick.models.common import (
    LazyJsonReference,
    Set,
    StrictBaseModel,
    ValidatedBaseModel,
)
from conda_forge_tick.models.conda_forge_yml import BuildPlatform, CondaForgeYml
from conda_forge_tick.models.meta_yaml import MetaYaml


class Requirements(StrictBaseModel):
    """
    A (generic or platform-specific) list of requirements taken from the `recipe/meta.yaml` file in the feedstock
    repository.
    The build, host, run, and run_constrained sections are identical to the corresponding sections in the `meta.yaml`
    file, whereas the test requirements section is taken from the test section in the `meta.yaml` file.
    Refer to https://docs.conda.io/projects/conda-build/en/stable/resources/define-metadata.html#requirements-section
    for a documentation of the fields in the meta.yaml file.
    """

    build: Set[str]
    host: Set[str]
    run: Set[str]
    run_constrained: Set[str] | None = None
    """
    This field is currently optional but should be required since no requirements can be represented by an empty set.
    A missing field should be treated as an empty set.
    """
    test: Set[str]


class BuildPlatformInfo(StrictBaseModel):
    meta_yaml: MetaYaml
    """
    A platform-specific representation of the `recipe/meta.yaml` file in the feedstock repository.
    All preprocessing selectors are resolved accordingly.
    https://docs.conda.io/projects/conda-build/en/stable/resources/define-metadata.html#preprocessing-selectors
    """

    requirements: Requirements
    """
    The platform-specific list of requirements taken from the `recipe/meta.yaml` file in the feedstock repository.
    All preprocessing selectors are resolved accordingly (see above).
    """


class NodeAttributesValid(StrictBaseModel):
    archived: bool
    """
    Is the feedstock repository archived?
    Archived feedstocks are excluded from most bot operations and never receive updates.
    """

    branch: str
    """
    The branch of the feedstock repository to track. This is usually the default branch of the feedstock repository.
    For new feedstocks, this defaults to `main`.
    """

    conda_forge_yml: CondaForgeYml = Field(..., alias="conda-forge.yml")
    """
    A parsed representation of the `conda-forge.yml` file in the feedstock repository.
    """

    feedstock_name: str
    """
    The name of the feedstock. If the GitHub feedstock repository has the name `foo-feedstock`,
    then the feedstock name is `foo`. Also, the node attributes JSON file is named `foo.json`.
    """

    hash_type: str | None = Field(None, examples=["sha256", "sha512", "md5"])
    """
    The type of hash used to verify the integrity of source archives. This is extracted from the source section of the
    `recipe/meta.yaml` file in the feedstock repository, as documented here:
    https://docs.conda.io/projects/conda-build/en/stable/resources/define-metadata.html#source-from-tarball-or-zip-archive
    The hash algorithm must be present in hashlib.algorithms_available.
    If multiple supported hash algorithms are present, the lexicographically largest one is chosen.
    If multiple sources are present, we consider the union (not: intersection) of all their hash types,
    which is probably not a good idea.

    If the sources section is missing, or all hash types are unsupported, this field is None (missing in the JSON).

    MD5 is obviously not recommended but used by a lot of feedstocks.
    """

    meta_yaml: MetaYaml
    """
    A unified representation of the `recipe/meta.yaml` file in the feedstock repository.
    The unified representation is not directly generated from the raw content of the `meta.yaml` file, but by
    concatenating the platform-specific fields in the `platform_info.meta_yaml` field.
    This can lead to duplicate entries if some platforms share some attributes, which is not good.
    """

    name: str
    """
    The package name, as extracted from the `meta.yaml` file in the feedstock repository.
    If different platforms have different package names (which should be unsupported), the package name is silently
    set to the package name of the lexicographically smallest platform. It would be better to raise an error in this
    case, but the current implementation does not do this.
    """

    outputs_names: Set[str]
    """
    The names of all outputs (packages) of the feedstock, as extracted from the outputs section of the `meta.yaml` file
    in the feedstock repository. If the outputs section is missing, this is set to the package name.
    If `meta.yaml` defines outputs and an implicit metapackage, the package name is also included in this set.

    Implicit metapackages documentation:
    https://docs.conda.io/projects/conda-build/en/stable/resources/define-metadata.html#implicit-metapackages
    """

    parsing_error: Literal[False]
    """
    Denotes an error that occurred while parsing the feedstock repository.
    If no error occurred, this is `False`.
    """

    platforms: set[BuildPlatform]
    """
    The list of build platforms (not: target platforms) this feedstock uses. For new feedstocks, this is inferred from
    the `*.yaml` files in the `.ci_support` directory of the feedstock repository. It consists of a platform name and
    an architecture name, separated by an underscore.

    If the .ci_support directory is missing or empty, this is set to `["win_64", "osx_64", "linux_64"]`, concatenated
    with the build platforms present in the `provider` section of the `conda-forge.yml` file. Duplicates are not
    removed (the current implementation uses a list), which is probably not a good idea (but the .ci_support directory
    should not be empty after all).
    """

    platform_info: dict[BuildPlatform, BuildPlatformInfo]
    """
    The build-specific information for each build platform in the `platforms` field.
    Note that this is represented differently in the old (JSON) model, see below.
    """

    # noinspection PyNestedDecorators
    @model_validator(mode="before")
    @classmethod
    def validate_platform_info(cls, data: Any) -> Any:
        """
        The current autotick-bot implementation makes use of `PLATFORM_meta_yaml` and `PLATFORM_requirements` fields
        that are present in this model, where PLATFORM is a build platform present in `platforms`.
        This data model is a bit too complex for what it does, so we transform it into a simpler model that is easier to
        work with. See platform_info above for the new model.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "We only support validating dicts. Pydantic supports calling model_validate with some "
                "other objects (e.g. in conjunction with construct), but we do not. "
                "See https://docs.pydantic.dev/latest/concepts/validators/#model-validators"
            )

        if "platform_info" in data:
            raise ValueError(
                "The `platform_info` field is reserved for the new model and must not be present in the old model."
            )

        data["platform_info"] = defaultdict(dict)
        for build_platform in BuildPlatform:
            if f"{build_platform}_meta_yaml" in data:
                data["platform_info"][build_platform]["meta_yaml"] = data.pop(
                    f"{build_platform}_meta_yaml"
                )
            if f"{build_platform}_requirements" in data:
                data["platform_info"][build_platform]["requirements"] = data.pop(
                    f"{build_platform}_requirements"
                )

        return data

    @model_validator(mode="after")
    def check_all_platform_infos_present(self) -> Self:
        """
        Ensure that the `platform_info` field is present for all build platforms in the `platforms` field.
        """
        if set(self.platform_info.keys()) != self.platforms:
            raise ValueError(
                "The `platform_info` field must contain all build platforms in the `platforms` field."
            )
        return self

    @model_serializer(mode="wrap")
    def serialize_platform_info(
        self, wrapped_serializer: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """
        Serialize the `platform_info` field into the old model.
        """

        serialized_model: dict[str, Any] = wrapped_serializer(self)

        serialized_model.update(
            {
                f"{build_platform}_meta_yaml": platform_info["meta_yaml"]
                for build_platform, platform_info in serialized_model[
                    "platform_info"
                ].items()
            }
        )
        serialized_model.update(
            {
                f"{build_platform}_requirements": platform_info["requirements"]
                for build_platform, platform_info in serialized_model[
                    "platform_info"
                ].items()
            }
        )

        del serialized_model["platform_info"]

        return serialized_model

    pr_info: LazyJsonReference
    """
    The JSON reference to the pull request information for the feedstock repository, created by migrators.
    Note that version updates are handled via the `version_pr_info` field.
    """

    raw_meta_yaml: str
    """
    The raw content of the `recipe/meta.yaml` file in the feedstock repository.
    """

    req: Set[str]
    """
    All requirements of the feedstock, as extracted from the `meta.yaml` file in the feedstock repository.
    This includes build, host, and run requirements, as well as all requirements defined in the outputs section,
    and is not further organized.
    """

    requirements: Requirements
    """
    The list of requirements taken from the `recipe/meta.yaml` file in the feedstock repository, organized
    by build, host, run, and test requirements. This includes all platforms.

    All requirements do not contain any version pins, but only package names.
    For example, `python >=3.6` is transformed into `python`.

    For a list of packages with version pins (e.g. `python >=3.6`), see `total_requirements`.
    """

    strong_exports: bool
    """
    True if the feedstocks defines at least one strong run export, False otherwise. Docs:
    https://docs.conda.io/projects/conda-build/en/stable/resources/define-metadata.html#export-runtime-requirements
    """

    time: float | None = None
    """
    A deprecated field which should be removed. In 03/2024 present for ~200 feedstocks.
    """

    total_requirements: Requirements
    """
    This is the same as `requirements`, but contains all requirements with version pins (if present).
    For example, `python >=3.6` stays `python >=3.6`.
    """

    url: AnyUrl | set[AnyUrl] | None = None
    """
    The upstream URL of the package source, extracted from the source section of the `meta.yaml` file in the feedstock
    repository. If no URL is present, this is None (in some cases missing in JSON, in some cases null in JSON).

    It looks like PipWheelMigrator currently uses this field incorrectly (ignoring the fact that this field can
    be a set of URLs). This should be fixed.
    """

    version: str | None = None
    """
    The package version of the feedstock, as extracted from the package.version field of the `meta.yaml` file in the
    feedstock repository.

    This is None if the `package.version` field is missing in the `meta.yaml` file, which can be valid if the outputs
    specify their own versions.

    Note that this field can have a value set if the `package.version` field is missing in the `meta.yaml` file, but
    a previous version of the feedstock had a `version` field set. This is not valid and should be fixed. Note that
    the version field can be outdated in this case!
    """

    @model_validator(mode="after")
    def check_version_match(self) -> Self:
        """
        Ensure that the version field matches the version field in the meta_yaml field.

        If both fields are None, all outputs must specify their own versions.
        """
        if self.version is None and self.meta_yaml.package.version is None:
            for output in self.meta_yaml.outputs or []:
                if output.version is None:
                    raise ValueError(
                        "If the `version` field is None, all outputs must specify their own versions."
                    )
            return self

        if self.version != self.meta_yaml.package.version:
            raise ValueError(
                "The `version` field must match the `package.version` field in the `meta_yaml` field."
            )

        return self

    version_pr_info: LazyJsonReference
    """
    The JSON reference to the pull request information for the feedstock repository, created by the version migrator.
    This is used to track version updates of the feedstock.

    The pull request information of all other migrations is tracked via the `pr_info` field.
    """


class NodeAttributesError(ValidatedBaseModel):
    """
    If a parsing error occurred, any number of fields can be missing.
    """

    parsing_error: str
    """
    Denotes an error that occurred while parsing the feedstock repository.
    """


NodeAttributes = TypeAdapter(NodeAttributesValid | NodeAttributesError)
