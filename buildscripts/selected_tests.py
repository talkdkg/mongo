#!/usr/bin/env python3
"""Command line utility for determining what jstests should run for the given changed files."""

import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import click
import structlog
from structlog.stdlib import LoggerFactory
from evergreen.api import EvergreenApi, RetryingEvergreenApi
from git import Repo
from shrub.config import Configuration
from shrub.variant import DisplayTaskDefinition, Variant

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pylint: disable=wrong-import-position
import buildscripts.resmokelib.parser
import buildscripts.util.read_config as read_config
from buildscripts.burn_in_tests import create_task_list_for_tests, is_file_a_test_file
from buildscripts.ciconfig.evergreen import (
    EvergreenProjectConfig,
    ResmokeArgs,
    Task,
    parse_evergreen_file,
)
from buildscripts.evergreen_generate_resmoke_tasks import (
    CONFIG_FORMAT_FN,
    DEFAULT_CONFIG_VALUES,
    REQUIRED_CONFIG_KEYS,
    ConfigOptions,
    GenerateSubSuites,
    remove_gen_suffix,
    write_file_dict,
)
from buildscripts.patch_builds.change_data import find_changed_files
from buildscripts.patch_builds.selected_tests_service import SelectedTestsService

structlog.configure(logger_factory=LoggerFactory())
LOGGER = structlog.getLogger(__name__)

EVERGREEN_FILE = "etc/evergreen.yml"
EVG_CONFIG_FILE = ".evergreen.yml"
EXTERNAL_LOGGERS = {
    "evergreen",
    "git",
    "urllib3",
}
SELECTED_TESTS_CONFIG_DIR = "selected_tests_config"
RELATION_THRESHOLD = 0.1

COMPILE_TASK_PATTERN = re.compile(".*compile.*")
CONCURRENCY_TASK_PATTERN = re.compile("concurrency.*")
INTEGRATION_TASK_PATTERN = re.compile("integration.*")
FUZZER_TASK_PATTERN = re.compile(".*fuzz.*")
GENERATE_TASK_PATTERN = re.compile("burn_in.*")
LINT_TASK_PATTERN = re.compile("lint.*")
STITCH_TASK_PATTERN = re.compile("stitch.*")
EXCLUDE_TASK_PATTERNS = [
    COMPILE_TASK_PATTERN, CONCURRENCY_TASK_PATTERN, INTEGRATION_TASK_PATTERN, FUZZER_TASK_PATTERN,
    GENERATE_TASK_PATTERN, LINT_TASK_PATTERN, STITCH_TASK_PATTERN
]

CPP_TASK_NAMES = [
    "dbtest",
    "idl_tests",
    "unittests",
]
PUBLISH_TASK_NAMES = [
    "package",
    "publish_packages",
    "push",
]
PYTHON_TESTS = ["buildscripts_test"]
EXCLUDE_TASK_LIST = [
    *CPP_TASK_NAMES,
    *PYTHON_TESTS,
    *PUBLISH_TASK_NAMES,
]


class SelectedTestsConfigOptions(ConfigOptions):
    """Retrieve configuration from a config file."""

    @classmethod
    # pylint: disable=too-many-arguments,W0221
    def from_file(cls, origin_variant_expansions: Dict[str, str], filepath: str,
                  overwrites: Dict[str, Any], required_keys: Set[str], defaults: Dict[str, Any],
                  formats: Dict[str, type]):
        """
        Create an instance of SelectedTestsConfigOptions based on the given config file.

        :param origin_variant_expansions: Expansions of the origin build variant.
        :param filepath: Path to file containing configuration.
        :param overwrites: Dict of configuration values to overwrite those listed in filepath.
        :param required_keys: Set of keys required by this config.
        :param defaults: Dict of default values for keys.
        :param formats: Dict with functions to format values before returning.
        :return: Instance of SelectedTestsConfigOptions.
        """
        config_from_file = read_config.read_config_file(filepath)
        return cls({**origin_variant_expansions, **config_from_file, **overwrites}, required_keys,
                   defaults, formats)

    @property
    def run_tests_task(self):
        """Return name of task name for s3 folder containing generated tasks config."""
        return remove_gen_suffix(self.name_of_generating_task)

    @property
    def run_tests_build_variant(self):
        """Return name of build_variant for s3 folder containing generated tasks config."""
        return self.name_of_generating_build_variant

    @property
    def run_tests_build_id(self):
        """Return name of build_id for s3 folder containing generated tasks config."""
        return self.name_of_generating_build_id

    @property
    def create_misc_suite(self):
        """Whether or not a _misc suite file should be created."""
        return not self.selected_tests_to_run

    def generate_display_task(self, task_names: List[str]) -> DisplayTaskDefinition:
        """
        Generate a display task with execution tasks.

        :param task_names: The names of the execution tasks to include under the display task.
        :return: Display task definition for the generated display task.
        """
        return DisplayTaskDefinition(f"{self.task}_{self.variant}").execution_tasks(task_names)


def _configure_logging(verbose: bool):
    """
    Configure logging for the application.

    :param verbose: If True set log level to DEBUG.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
        level=level,
        stream=sys.stdout,
    )
    for log_name in EXTERNAL_LOGGERS:
        logging.getLogger(log_name).setLevel(logging.WARNING)


def _find_selected_test_files(
        selected_tests_service: SelectedTestsService,
        changed_files: Set[str],
) -> Set[str]:
    """
    Request related test files from selected-tests service and filter invalid files.

    :param selected_tests_service: Selected-tests service.
    :param changed_files: Set of changed_files.
    :return: Set of test files returned by selected-tests service that are valid test files.
    """
    test_mappings = selected_tests_service.get_test_mappings(RELATION_THRESHOLD, changed_files)
    return {
        test_file["name"]
        for test_mapping in test_mappings for test_file in test_mapping["test_files"]
        if is_file_a_test_file(test_file["name"])
    }


def _find_selected_tasks(selected_tests_service: SelectedTestsService, changed_files: Set[str],
                         build_variant_config: Variant) -> Set[str]:
    """
    Request tasks from selected-tests and filter out tasks that don't exist or should be excluded.

    :param selected_tests_service: Selected-tests service.
    :param changed_files: Set of changed_files.
    :param build_variant_config: Config of build variant to collect task info from.
    :return: Set of tasks returned by selected-tests service that should not be excluded.
    """
    task_mappings = selected_tests_service.get_task_mappings(RELATION_THRESHOLD, changed_files)
    returned_task_names = {
        task["name"]
        for task_mapping in task_mappings for task in task_mapping["tasks"]
    }
    existing_task_names = set()
    for task_name in returned_task_names:
        task = _find_task(build_variant_config, task_name)
        if task:
            if task.name in EXCLUDE_TASK_LIST or any(
                    regex.match(task.name) for regex in EXCLUDE_TASK_PATTERNS):
                LOGGER.debug("Excluding task from analysis because it is not a jstest",
                             task=task_name)
                continue
            existing_task_names.add(task.name)
    return existing_task_names


def _find_task(build_variant_config: Variant, task_name: str) -> Task:
    """
    Look up shrub config for task.

    :param build_variant_config: Config of build variant to collect task info from.
    :param task_name: Name of task to get info for.
    :return: Task configuration.
    """
    task = build_variant_config.get_task(task_name)
    if not task:
        task = build_variant_config.get_task(task_name + "_gen")
    return task


def _get_selected_tests_task_config(expansion_file: str) -> Dict[str, str]:
    """
    Look up task config of the selected tests task.

    :param expansion_file: Configuration file.
    :return: Task configuration values.
    """
    expansions = read_config.read_config_file(expansion_file)
    return {
        "name_of_generating_task": expansions["task_name"],
        "name_of_generating_build_variant": expansions["build_variant"],
        "name_of_generating_build_id": expansions["build_id"]
    }


def _get_evg_task_config(
        expansion_file: str,
        task_name: str,
        build_variant_config: Variant,
) -> Dict[str, Any]:
    """
    Look up task config of the task to be generated.

    :param expansion_file: Configuration file.
    :param task_name: Task to get info for.
    :param build_variant_config: Config of build variant to collect task info from.
    :return: Task configuration values.
    """
    task = build_variant_config.get_task(task_name)
    if task.is_generate_resmoke_task:
        task_vars = task.generate_resmoke_tasks_command["vars"]
    else:
        task_vars = task.run_tests_command["vars"]
        task_vars.update({"fallback_num_sub_suites": "1"})

    suite_name = ResmokeArgs.get_arg(task_vars["resmoke_args"], "suites")
    if suite_name:
        task_vars.update({"suite": suite_name})

    # the suites argument will run all tests in a suite even when individual
    # tests are specified in resmoke_args, so we remove it
    resmoke_args_without_suites = ResmokeArgs.remove_arg(task_vars["resmoke_args"], "suites")
    task_vars["resmoke_args"] = resmoke_args_without_suites

    selected_tests_task_config = _get_selected_tests_task_config(expansion_file)

    return {
        "task_name": task.name, "build_variant": build_variant_config.name, **task_vars,
        **selected_tests_task_config
    }


def _update_config_with_task(evg_api: EvergreenApi, shrub_config: Configuration,
                             config_options: SelectedTestsConfigOptions,
                             config_dict_of_suites_and_tasks: Dict[str, str]):
    """
    Generate the suites config and the task shrub config for a given task config.

    :param evg_api: Evergreen API object.
    :param shrub_config: Shrub configuration for task.
    :param config_options: Task configuration options.
    :param config_dict_of_suites_and_tasks: Dict of shrub configs and suite file contents.
    """
    task_generator = GenerateSubSuites(evg_api, config_options)
    suites = task_generator.get_suites()

    config_dict_of_suites = task_generator.generate_suites_config(suites)
    config_dict_of_suites_and_tasks.update(config_dict_of_suites)

    task_generator.generate_task_config(shrub_config, suites)


def _get_task_configs_for_test_mappings(expansion_file: str, tests_by_task: Dict[str, Any],
                                        build_variant_config: Variant) -> Dict[str, dict]:
    """
    For test mappings, generate a dict containing task names and their config settings.

    :param expansion_file: Configuration file.
    :param tests_by_task: Dictionary of tests and tasks to run.
    :param build_variant_config: Config of build variant to collect task info from.
    :return: Dict of task names and their config settings.
    """
    evg_task_configs = {}
    for task_name, test_list_info in tests_by_task.items():
        evg_task_config = _get_evg_task_config(expansion_file, task_name, build_variant_config)
        evg_task_config.update({"selected_tests_to_run": set(test_list_info["tests"])})
        LOGGER.debug("Calculated evg_task_config values", evg_task_config=evg_task_config)
        evg_task_configs[task_name] = evg_task_config

    return evg_task_configs


def _get_task_configs_for_task_mappings(expansion_file: str, related_tasks: List[str],
                                        build_variant_config: Variant) -> Dict[str, dict]:
    """
    For task mappings, generate a dict containing task names and their config settings.

    :param expansion_file: Configuration file.
    :param related_tasks: List of tasks to run.
    :param build_variant_config: Config of build variant to collect task info from.
    :return: Dict of task names and their config settings.
    """
    evg_task_configs = {}
    for task_name in related_tasks:
        evg_task_config = _get_evg_task_config(expansion_file, task_name, build_variant_config)
        LOGGER.debug("Calculated evg_task_config values", evg_task_config=evg_task_config)
        evg_task_configs[task_name] = evg_task_config

    return evg_task_configs


# pylint: disable=too-many-arguments, too-many-locals
def run(evg_api: EvergreenApi, evg_conf: EvergreenProjectConfig, expansion_file: str,
        selected_tests_service: SelectedTestsService, changed_files: Set[str],
        build_variant: str) -> Dict[str, dict]:
    """
    Run code to select tasks to run based on test mappings and task mappings.

    :param evg_api: Evergreen API object.
    :param evg_conf: Evergreen configuration.
    :param expansion_file: Configuration file.
    :param selected_tests_config: Location of config file to connect to elected-tests service.
    :param changed_files: Set of changed_files.
    :param build_variant: Build variant to collect task info from.
    :return: Dict of files and file contents for generated tasks.
    """
    shrub_config = Configuration()
    config_dict_of_suites_and_tasks = {}

    task_configs = {}
    build_variant_config = evg_conf.get_variant(build_variant)

    related_test_files = _find_selected_test_files(selected_tests_service, changed_files)
    LOGGER.debug("related test files found", related_test_files=related_test_files)
    if related_test_files:
        tests_by_task = create_task_list_for_tests(related_test_files, build_variant, evg_conf)
        LOGGER.debug("tests and tasks found", tests_by_task=tests_by_task)

        test_mapping_task_configs = _get_task_configs_for_test_mappings(
            expansion_file, tests_by_task, build_variant_config)
        task_configs.update(test_mapping_task_configs)

    related_tasks = _find_selected_tasks(selected_tests_service, changed_files,
                                         build_variant_config)
    LOGGER.debug("related tasks found", related_tasks=related_tasks)
    if related_tasks:
        task_mapping_task_configs = _get_task_configs_for_task_mappings(
            expansion_file, related_tasks, build_variant_config)
        # task_mapping_task_configs will overwrite test_mapping_task_configs
        # because task_mapping_task_configs will run all tests rather than a subset of tests and we
        # should err on the side of running all tests
        task_configs.update(task_mapping_task_configs)

    origin_variant_expansions = build_variant_config.expansions

    for task_config in task_configs.values():
        config_options = SelectedTestsConfigOptions.from_file(
            origin_variant_expansions,
            expansion_file,
            task_config,
            REQUIRED_CONFIG_KEYS,
            DEFAULT_CONFIG_VALUES,
            CONFIG_FORMAT_FN,
        )
        _update_config_with_task(evg_api, shrub_config, config_options,
                                 config_dict_of_suites_and_tasks)

    config_dict_of_suites_and_tasks["selected_tests_config.json"] = shrub_config.to_json()
    return config_dict_of_suites_and_tasks


@click.command()
@click.option("--verbose", "verbose", default=False, is_flag=True, help="Enable extra logging.")
@click.option(
    "--expansion-file",
    "expansion_file",
    type=str,
    required=True,
    help="Location of expansions file generated by evergreen.",
)
@click.option(
    "--evg-api-config",
    "evg_api_config",
    default=EVG_CONFIG_FILE,
    metavar="FILE",
    help="Configuration file with connection info for Evergreen API.",
)
@click.option(
    "--build-variant",
    "build_variant",
    required=True,
    help="Tasks to run will be selected from this build variant.",
)
@click.option(
    "--selected-tests-config",
    "selected_tests_config",
    required=True,
    metavar="FILE",
    help="Configuration file with connection info for selected tests service.",
)
def main(
        verbose: bool,
        expansion_file: str,
        evg_api_config: str,
        build_variant: str,
        selected_tests_config: str,
):
    """
    Select tasks to be run based on changed files in a patch build.

    :param verbose: Log extra debug information.
    :param expansion_file: Configuration file.
    :param evg_api_config: Location of configuration file to connect to evergreen.
    :param build_variant: Build variant to query tasks from.
    :param selected_tests_config: Location of config file to connect to elected-tests service.
    """
    _configure_logging(verbose)

    evg_api = RetryingEvergreenApi.get_api(config_file=evg_api_config)
    evg_conf = parse_evergreen_file(EVERGREEN_FILE)
    selected_tests_service = SelectedTestsService.from_file(selected_tests_config)

    repo = Repo(".")
    changed_files = find_changed_files(repo)
    buildscripts.resmokelib.parser.set_options()
    LOGGER.debug("Found changed files", files=changed_files)

    config_dict_of_suites_and_tasks = run(evg_api, evg_conf, expansion_file, selected_tests_service,
                                          changed_files, build_variant)
    write_file_dict(SELECTED_TESTS_CONFIG_DIR, config_dict_of_suites_and_tasks)


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
