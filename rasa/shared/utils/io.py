from collections import OrderedDict
import errno
import glob
from hashlib import md5
from io import StringIO
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Text, Type, Union, FrozenSet
import warnings

from ruamel import yaml as yaml
from ruamel.yaml import RoundTripRepresenter, YAMLError
from ruamel.yaml.constructor import DuplicateKeyError

from rasa.shared.constants import (
    DEFAULT_LOG_LEVEL,
    ENV_LOG_LEVEL,
    NEXT_MAJOR_VERSION_FOR_DEPRECATIONS,
    CONFIG_SCHEMA_FILE,
    MODEL_CONFIG_SCHEMA_FILE,
)
from rasa.shared.exceptions import (
    FileIOException,
    FileNotFoundException,
    YamlSyntaxException,
)
import rasa.shared.utils.validation

DEFAULT_ENCODING = "utf-8"
YAML_VERSION = (1, 2)


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def wrap_with_color(*args: Any, color: Text) -> Text:
    return color + " ".join(str(s) for s in args) + bcolors.ENDC


def raise_warning(
    message: Text,
    category: Optional[Type[Warning]] = None,
    docs: Optional[Text] = None,
    **kwargs: Any,
) -> None:
    """Emit a `warnings.warn` with sensible defaults and a colored warning msg."""

    original_formatter = warnings.formatwarning

    def should_show_source_line() -> bool:
        if "stacklevel" not in kwargs:
            if category == UserWarning or category is None:
                return False
            if category == FutureWarning:
                return False
        return True

    def formatwarning(
        message: Text,
        category: Optional[Type[Warning]],
        filename: Text,
        lineno: Optional[int],
        line: Optional[Text] = None,
    ):
        """Function to format a warning the standard way."""

        if not should_show_source_line():
            if docs:
                line = f"More info at {docs}"
            else:
                line = ""

        formatted_message = original_formatter(
            message, category, filename, lineno, line
        )
        return wrap_with_color(formatted_message, color=bcolors.WARNING)

    if "stacklevel" not in kwargs:
        # try to set useful defaults for the most common warning categories
        if category == DeprecationWarning:
            kwargs["stacklevel"] = 3
        elif category in (UserWarning, FutureWarning):
            kwargs["stacklevel"] = 2

    warnings.formatwarning = formatwarning
    warnings.warn(message, category=category, **kwargs)
    warnings.formatwarning = original_formatter


def write_text_file(
    content: Text,
    file_path: Union[Text, Path],
    encoding: Text = DEFAULT_ENCODING,
    append: bool = False,
) -> None:
    """Writes text to a file.

    Args:
        content: The content to write.
        file_path: The path to which the content should be written.
        encoding: The encoding which should be used.
        append: Whether to append to the file or to truncate the file.

    """
    mode = "a" if append else "w"
    with open(file_path, mode, encoding=encoding) as file:
        file.write(content)


def read_file(filename: Union[Text, Path], encoding: Text = DEFAULT_ENCODING) -> Any:
    """Read text from a file."""

    try:
        with open(filename, encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundException(
            f"Failed to read file, " f"'{os.path.abspath(filename)}' does not exist."
        )
    except UnicodeDecodeError:
        raise FileIOException(
            f"Failed to read file '{os.path.abspath(filename)}', "
            f"could not read the file using {encoding} to decode "
            f"it. Please make sure the file is stored with this "
            f"encoding."
        )


def read_json_file(filename: Union[Text, Path]) -> Any:
    """Read json from a file."""
    content = read_file(filename)
    try:
        return json.loads(content)
    except ValueError as e:
        raise FileIOException(
            f"Failed to read json from '{os.path.abspath(filename)}'. Error: {e}"
        )


def list_directory(path: Text) -> List[Text]:
    """Returns all files and folders excluding hidden files.

    If the path points to a file, returns the file. This is a recursive
    implementation returning files in any depth of the path."""

    if not isinstance(path, str):
        raise ValueError(
            f"`resource_name` must be a string type. " f"Got `{type(path)}` instead"
        )

    if os.path.isfile(path):
        return [path]
    elif os.path.isdir(path):
        results = []
        for base, dirs, files in os.walk(path, followlinks=True):
            # sort files for same order across runs
            files = sorted(files, key=_filename_without_prefix)
            # add not hidden files
            good_files = filter(lambda x: not x.startswith("."), files)
            results.extend(os.path.join(base, f) for f in good_files)
            # add not hidden directories
            good_directories = filter(lambda x: not x.startswith("."), dirs)
            results.extend(os.path.join(base, f) for f in good_directories)
        return results
    else:
        raise ValueError(f"Could not locate the resource '{os.path.abspath(path)}'.")


def list_files(path: Text) -> List[Text]:
    """Returns all files excluding hidden files.

    If the path points to a file, returns the file."""

    return [fn for fn in list_directory(path) if os.path.isfile(fn)]


def _filename_without_prefix(file: Text) -> Text:
    """Splits of a filenames prefix until after the first ``_``."""
    return "_".join(file.split("_")[1:])


def list_subdirectories(path: Text) -> List[Text]:
    """Returns all folders excluding hidden files.

    If the path points to a file, returns an empty list."""

    return [fn for fn in glob.glob(os.path.join(path, "*")) if os.path.isdir(fn)]


def deep_container_fingerprint(
    obj: Union[List[Any], Dict[Any, Any]], encoding: Text = DEFAULT_ENCODING
) -> Text:
    """Calculate a hash which is stable, independent of a containers key order.

    Works for lists and dictionaries. For keys and values, we recursively call
    `hash(...)` on them. Keep in mind that a list with keys in a different order
    will create the same hash!

    Args:
        obj: dictionary or list to be hashed.
        encoding: encoding used for dumping objects as strings

    Returns:
        hash of the container.
    """
    if isinstance(obj, dict):
        return get_dictionary_fingerprint(obj, encoding)
    if isinstance(obj, list):
        return get_list_fingerprint(obj, encoding)
    else:
        return get_text_hash(str(obj), encoding)


def get_dictionary_fingerprint(
    dictionary: Dict[Any, Any], encoding: Text = DEFAULT_ENCODING
) -> Text:
    """Calculate the fingerprint for a dictionary.

    The dictionary can contain any keys and values which are either a dict,
    a list or a elements which can be dumped as a string.

    Args:
        dictionary: dictionary to be hashed
        encoding: encoding used for dumping objects as strings

    Returns:
        The hash of the dictionary
    """
    stringified = json.dumps(
        {
            deep_container_fingerprint(k, encoding): deep_container_fingerprint(
                v, encoding
            )
            for k, v in dictionary.items()
        },
        sort_keys=True,
    )
    return get_text_hash(stringified, encoding)


def get_list_fingerprint(
    elements: List[Any], encoding: Text = DEFAULT_ENCODING
) -> Text:
    """Calculate a fingerprint for an unordered list.

    Args:
        elements: unordered list
        encoding: encoding used for dumping objects as strings

    Returns:
        the fingerprint of the list
    """
    stringified = json.dumps(
        [deep_container_fingerprint(element, encoding) for element in elements]
    )
    return get_text_hash(stringified, encoding)


def get_text_hash(text: Text, encoding: Text = DEFAULT_ENCODING) -> Text:
    """Calculate the md5 hash for a text."""
    return md5(text.encode(encoding)).hexdigest()  # nosec


def json_to_string(obj: Any, **kwargs: Any) -> Text:
    """Dumps a JSON-serializable object to string.

    Args:
        obj: JSON-serializable object.
        kwargs: serialization options. Defaults to 2 space indentation
                and disable escaping of non-ASCII characters.

    Returns:
        The objects serialized to JSON, as a string.
    """
    indent = kwargs.pop("indent", 2)
    ensure_ascii = kwargs.pop("ensure_ascii", False)
    return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, **kwargs)


def fix_yaml_loader() -> None:
    """Ensure that any string read by yaml is represented as unicode."""

    def construct_yaml_str(self, node):
        # Override the default string handling function
        # to always return unicode objects
        return self.construct_scalar(node)

    yaml.Loader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)
    yaml.SafeLoader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)
    yaml.allow_duplicate_keys = False


def replace_environment_variables() -> None:
    """Enable yaml loader to process the environment variables in the yaml."""
    # eg. ${USER_NAME}, ${PASSWORD}
    env_var_pattern = re.compile(r"^(.*)\$\{(.*)\}(.*)$")
    yaml.Resolver.add_implicit_resolver("!env_var", env_var_pattern, None)

    def env_var_constructor(loader, node):
        """Process environment variables found in the YAML."""
        value = loader.construct_scalar(node)
        expanded_vars = os.path.expandvars(value)
        if "$" in expanded_vars:
            not_expanded = [w for w in expanded_vars.split() if "$" in w]
            raise ValueError(
                "Error when trying to expand the environment variables"
                " in '{}'. Please make sure to also set these environment"
                " variables: '{}'.".format(value, not_expanded)
            )
        return expanded_vars

    yaml.SafeConstructor.add_constructor("!env_var", env_var_constructor)


fix_yaml_loader()
_parsers: Dict[Union[Text, FrozenSet[Text]], Any] = {}
_default_yaml_implicit_resolvers = yaml.Resolver.yaml_implicit_resolvers.copy()
_default_yaml_constructors = yaml.SafeConstructor.yaml_constructors.copy()
ENV_VAR_REGEX = re.compile(r"\$\{[\S]+\}")


class YAMLParser(yaml.YAML):
    """A custom parser for YAML.

    It's used because `ruamel.yaml` stores all its information globally.
    Which means it's impossible to create different parsers with different constructors
    and resolvers without remembering the previous configuration and restoring it once
    it's needed. That's exactly what this class does.
    """

    def __init__(
        self,
        reader_type: Union[Text, List[Text]] = "safe",
        replace_env_vars: bool = False,
    ) -> None:
        """Creates an instance of YAMLParser.

        Args:
            reader_type: YAML reader type.
            replace_env_vars: Indicates if environment variables need to be replaced.
        """
        super().__init__(typ=reader_type)

        if replace_env_vars:
            replace_environment_variables()
        else:
            # we need to do this because `yaml` adds contains a global `implicit_resolvers` list
            # and the resolves from there cannot be de-registered
            yaml.SafeConstructor.add_constructor("!env_var", lambda _, node: node.value)

        self._save_modified_yaml_parameters()
        self.restore_default_yaml_parameters()

    @staticmethod
    def restore_default_yaml_parameters() -> None:
        """Restores the `ruamel.yaml` parameters that were specified before."""
        yaml.Resolver.yaml_implicit_resolvers = _default_yaml_implicit_resolvers
        yaml.SafeConstructor.yaml_constructors = _default_yaml_constructors

    def _save_modified_yaml_parameters(self) -> None:
        self._modified_yaml_implicit_resolvers = (
            yaml.Resolver.yaml_implicit_resolvers.copy()
        )
        self._modified_yaml_constructors = yaml.SafeConstructor.yaml_constructors.copy()

    def _restore_modified_yaml_parameters(self) -> None:
        yaml.Resolver.yaml_implicit_resolvers = self._modified_yaml_implicit_resolvers
        yaml.SafeConstructor.yaml_constructors = self._modified_yaml_constructors

    def load(self, stream: Union[Path, Any]) -> Any:
        """Loads the YAML content.

        Args:
            stream: Any content or stream to parse.

        Returns:
            Parsed YAML.
        """
        self._restore_modified_yaml_parameters()
        return super().load(stream)


def _get_yaml_parser(
    reader_type: Union[Text, List[Text]] = "safe", replace_env_vars: bool = False
) -> YAMLParser:
    # we cannot reuse the existing parser in `_parsers` when `replace_env_vars` is `True`
    # because the set of env variables might change
    if replace_env_vars:
        return YAMLParser(reader_type=reader_type, replace_env_vars=True)

    key = frozenset(reader_type) if isinstance(reader_type, list) else reader_type
    if key in _parsers:
        return _parsers[key]

    return _parsers.setdefault(key, YAMLParser(reader_type=reader_type))


def read_yaml(content: Text, reader_type: Union[Text, List[Text]] = "safe") -> Any:
    """Parses yaml from a text.

    Args:
        content: A text containing yaml content.
        reader_type: Reader type to use. By default "safe" will be used
        replace_env_vars: Specifies if environment variables need to be replaced

    Raises:
        ruamel.yaml.parser.ParserError: If there was an error when parsing the YAML.
    """
    if _is_ascii(content):
        # Required to make sure emojis are correctly parsed
        content = (
            content.encode("utf-8")
            .decode("raw_unicode_escape")
            .encode("utf-16", "surrogatepass")
            .decode("utf-16")
        )

    yaml_parser = _get_yaml_parser(reader_type, bool(ENV_VAR_REGEX.search(content)))
    yaml_parser.version = YAML_VERSION
    yaml_parser.preserve_quotes = True

    content = yaml_parser.load(content) or {}
    YAMLParser.restore_default_yaml_parameters()
    return content


def _is_ascii(text: Text) -> bool:
    return all(ord(character) < 128 for character in text)


def read_yaml_file(filename: Union[Text, Path]) -> Union[List[Any], Dict[Text, Any]]:
    """Parses a yaml file.

    Raises an exception if the content of the file can not be parsed as YAML.

    Args:
        filename: The path to the file which should be read.

    Returns:
        Parsed content of the file.
    """
    try:
        return read_yaml(read_file(filename, DEFAULT_ENCODING))
    except (YAMLError, DuplicateKeyError) as e:
        raise YamlSyntaxException(filename, e)


def write_yaml(
    data: Any,
    target: Union[Text, Path, StringIO],
    should_preserve_key_order: bool = False,
) -> None:
    """Writes a yaml to the file or to the stream

    Args:
        data: The data to write.
        target: The path to the file which should be written or a stream object
        should_preserve_key_order: Whether to force preserve key order in `data`.
    """
    _enable_ordered_dict_yaml_dumping()

    if should_preserve_key_order:
        data = convert_to_ordered_dict(data)

    dumper = yaml.YAML()
    # no wrap lines
    dumper.width = YAML_LINE_MAX_WIDTH

    # use `null` to represent `None`
    dumper.representer.add_representer(
        type(None),
        lambda self, _: self.represent_scalar("tag:yaml.org,2002:null", "null"),
    )

    if isinstance(target, StringIO):
        dumper.dump(data, target)
        return

    with Path(target).open("w", encoding=DEFAULT_ENCODING) as outfile:
        dumper.dump(data, outfile)


YAML_LINE_MAX_WIDTH = 4096


def convert_to_ordered_dict(obj: Any) -> Any:
    """Convert object to an `OrderedDict`.

    Args:
        obj: Object to convert.

    Returns:
        An `OrderedDict` with all nested dictionaries converted if `obj` is a
        dictionary, otherwise the object itself.
    """
    if isinstance(obj, OrderedDict):
        return obj
    # use recursion on lists
    if isinstance(obj, list):
        return [convert_to_ordered_dict(element) for element in obj]

    if isinstance(obj, dict):
        out = OrderedDict()
        # use recursion on dictionaries
        for k, v in obj.items():
            out[k] = convert_to_ordered_dict(v)

        return out

    # return all other objects
    return obj


def _enable_ordered_dict_yaml_dumping() -> None:
    """Ensure that `OrderedDict`s are dumped so that the order of keys is respected."""
    yaml.add_representer(
        OrderedDict,
        RoundTripRepresenter.represent_dict,
        representer=RoundTripRepresenter,
    )


def is_logging_disabled() -> bool:
    """Returns `True` if log level is set to WARNING or ERROR, `False` otherwise."""
    log_level = os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)

    return log_level in ("ERROR", "WARNING")


def create_directory_for_file(file_path: Union[Text, Path]) -> None:
    """Creates any missing parent directories of this file path."""

    create_directory(os.path.dirname(file_path))


def dump_obj_as_json_to_file(filename: Union[Text, Path], obj: Any) -> None:
    """Dump an object as a json string to a file."""

    write_text_file(json.dumps(obj, indent=2), filename)


def dump_obj_as_yaml_to_string(
    obj: Any, should_preserve_key_order: bool = False
) -> Text:
    """Writes data (python dict) to a yaml string.

    Args:
        obj: The object to dump. Has to be serializable.
        should_preserve_key_order: Whether to force preserve key order in `data`.

    Returns:
        The object converted to a YAML string.
    """
    buffer = StringIO()

    write_yaml(obj, buffer, should_preserve_key_order=should_preserve_key_order)

    return buffer.getvalue()


def create_directory(directory_path: Text) -> None:
    """Creates a directory and its super paths.

    Succeeds even if the path already exists."""

    try:
        os.makedirs(directory_path)
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            raise


def raise_deprecation_warning(
    message: Text,
    warn_until_version: Text = NEXT_MAJOR_VERSION_FOR_DEPRECATIONS,
    docs: Optional[Text] = None,
    **kwargs: Any,
) -> None:
    """
    Thin wrapper around `raise_warning()` to raise a deprecation warning. It requires
    a version until which we'll warn, and after which the support for the feature will
    be removed.
    """
    if warn_until_version not in message:
        message = f"{message} (will be removed in {warn_until_version})"

    # need the correct stacklevel now
    kwargs.setdefault("stacklevel", 3)
    # we're raising a `FutureWarning` instead of a `DeprecationWarning` because
    # we want these warnings to be visible in the terminal of our users
    # https://docs.python.org/3/library/warnings.html#warning-categories
    raise_warning(message, FutureWarning, docs, **kwargs)


def read_validated_yaml(filename: Union[Text, Path], schema: Text) -> Any:
    """Validates YAML file content and returns parsed content.

    Args:
        filename: The path to the file which should be read.
        schema: The path to the schema file which should be used for validating the
            file content.

    Returns:
        The parsed file content.

    Raises:
        YamlValidationException: In case the model configuration doesn't match the
            expected schema.
    """
    content = read_file(filename)

    rasa.shared.utils.validation.validate_yaml_schema(content, schema)
    return read_yaml(content)


def read_config_file(filename: Union[Path, Text]) -> Dict[Text, Any]:
    """Parses a yaml configuration file. Content needs to be a dictionary.

    Args:
        filename: The path to the file which should be read.

    Raises:
        YamlValidationException: In case file content is not a `Dict`.

    Returns:
        Parsed config file.
    """
    return read_validated_yaml(filename, CONFIG_SCHEMA_FILE)


def read_model_configuration(filename: Union[Path, Text]) -> Dict[Text, Any]:
    """Parses a model configuration file.

    Args:
        filename: The path to the file which should be read.

    Raises:
        YamlValidationException: In case the model configuration doesn't match the
            expected schema.

    Returns:
        Parsed config file.
    """
    return read_validated_yaml(filename, MODEL_CONFIG_SCHEMA_FILE)


def is_subdirectory(path: Text, potential_parent_directory: Text) -> bool:
    """Checks if `path` is a subdirectory of `potential_parent_directory`.

    Args:
        path: Path to a file or directory.
        potential_parent_directory: Potential parent directory.

    Returns:
        `True` if `path` is a subdirectory of `potential_parent_directory`.
    """
    if path is None or potential_parent_directory is None:
        return False

    path = os.path.abspath(path)
    potential_parent_directory = os.path.abspath(potential_parent_directory)

    return potential_parent_directory in path
