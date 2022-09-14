import os
import subprocess
import sys
import tarfile
from collections import namedtuple
from io import BytesIO
from tempfile import TemporaryDirectory

import docker
import pytest
from cookiecutter import main as ck
from cookiecutter.generate import generate_context
from cookiecutter.prompt import prompt_for_config
from pytest_cases import fixture, parametrize, parametrize_with_cases

PYTHON_VERSION = "3.9"
EXTRA_CONTEXT = {"python_version": PYTHON_VERSION}


# The config in cookiecutter.json, once expanded
DEFAULT_CONF = prompt_for_config(
    generate_context(extra_context=EXTRA_CONTEXT), no_input=True
)

DOCKER_CLIENT = docker.from_env()

DOCKER_DEVIMG_NAME = "python-skeleton-testing"


Template = namedtuple("Template", ["path", "context"])


def print_docker_build_log(build_log_stream):
    """Convert a docker build log JSON stream to string"""
    log_item = next(build_log_stream)
    log_acc = ""
    # Exhaust the "stream" key while we aren't erroring
    while "error" not in log_item:
        log_acc += log_item["stream"]
        if "\n" in log_acc:  # TODO: Print separate newline on multiple \n in log_acc
            print(log_acc)
            log_acc = ""
        log_item = next(build_log_stream)
    # Reached the error: print it and quit
    print(log_item["error"], file=sys.stderr)
    print(log_item["errorDetail"], file=sys.stderr)


def copy_container_path_out(container, path, destination):
    """Copy a container's path out into destination/"""
    tar_stream, stats = container.get_archive(path, encode_stream=True)
    tar_bytestring = BytesIO()
    for chunk in tar_stream:
        tar_bytestring.write(chunk)
    tar_bytestring.seek(0)  # Rewind the in-memory tar file for data-extraction
    with tarfile.open(fileobj=tar_bytestring, mode="r") as tar_file:
        tar_file.extractall(destination)


@fixture
@parametrize(python_version=[PYTHON_VERSION])  # "3.7", "3.8", "3.10",
def template(python_version):
    """Template expansion fixture, parametrized by python version"""
    extra_context = {"python_version": python_version}
    conf = prompt_for_config(
        generate_context(extra_context=extra_context), no_input=True
    )
    with TemporaryDirectory() as tmp_path:
        path = expand_template(tmp_path, extra_context)
        yield Template(path, conf)


def python_dev_image(template_path):
    """Build the python image of the template's dockerfile"""
    try:
        img = DOCKER_CLIENT.images.build(
            path=str(template_path),
            tag=DOCKER_DEVIMG_NAME,
            # buildargs={"USERID": str(os.getuid())},
            rm=True,
        )
    except (docker.errors.BuildError) as e:
        print(f"Failed to build the main templated Dockerfile. Build log:")
        print_docker_build_log(e.build_log)
        raise e
    return DOCKER_DEVIMG_NAME


def expand_template(tmp_path, extra_context=EXTRA_CONTEXT):
    """Expand a single template"""
    return ck.cookiecutter(
        ".", extra_context=extra_context, output_dir=tmp_path, no_input=True
    )


def docker_run_devimg(command, workdir, raise_on_nonzero_exitcode=True):
    """Run the given command in the dev image


    Emulate a docker build + docker run + docker cp via docker-py
    """
    python_dev_image(workdir)
    try:
        container = DOCKER_CLIENT.containers.run(
            image=DOCKER_DEVIMG_NAME,
            command=command,
            volumes=[
                # f"{workdir}:/app",
                # Named volume mount for ownership
                f"pyskel-{PYTHON_VERSION}:/caches:rw",
                # "/etc/passwd:/etc/passwd:ro",
                # "/etc/groups:/etc/groups:ro",
            ],
            # working_dir="/app",
            stdout=True,
            stderr=True,
            # read_only=False,
            # user=os.getuid(),  # Avoid root-owned permissions issues
            # group_add=[os.getgid()],
            environment={"XDG_CACHE_HOME": "/caches/"},
            # auto_remove=True,
            detach=True,
        )
        # Block till container completed
        response = container.wait(timeout=90)
        exit_code = response["StatusCode"]
        print(container.logs())
        if exit_code > 0:
            pytest.fail(f"Build unsuccessful, container exited {exit_code}")
        # Successful run: copy data back out for analysis
        copy_source_path = "/workdir"
        copy_container_path_out(container, copy_source_path, workdir)
        return workdir + copy_source_path
    except (
        docker.errors.ContainerError,
        docker.errors.APIError,
    ) as e:
        # Explicitly get container's logs, since it may be empty
        if raise_on_nonzero_exitcode:
            logs = DOCKER_CLIENT.containers.get(e.container.name).logs()
            pytest.fail(
                f"Failed running {command} error was: {e}. Container logs: {logs}"
            )


def tests_template_renders_ok(template):
    """Checks we can invoke cookiecutter simply without specific arguments"""
    pass  # Checking the "template" fixture doesn't fail the test


def tests_template_packages_ok(template):
    """Checks we can run poetry build on rendered code to get a binary"""
    out_path = docker_run_devimg(["poetry", "build"], template.path)
    assert os.listdir(out_path + "/dist/"), "Nothing was built!"


def tests_template_docs_ok(template):
    """Checks we can run make docson rendered code to get HTML"""
    out_path = docker_run_devimg(["make", "docs"], template.path)
    assert os.listdir(out_path + "/docs/build/html/"), "Docs not built"


def tests_template_makes_ok(template):
    """Checks we can run make on rendered code to get a binary/tests"""
    out_path = docker_run_devimg("make", template.path)
    assert os.listdir(out_path + "/dist/"), "Nothing was built!"
    assert os.path.isfile(
        out_path + "/test_results/results.xml"
    ), "Test results not saved"
    assert os.path.isfile(
        out_path + "/test_results/coverage.xml"
    ), "Coverage report not saved"
    assert os.path.isfile(
        out_path + "/test_results/flake8.txt"
    ), "Linter results not saved"
    git_changes_post_make = subprocess.run(
        ["git", "status", "--short"],
        cwd=out_path,
        capture_output=True,
        text=True,
    )
    assert (
        not git_changes_post_make.stdout
    ), "Git found unstaged files after running 'make'"


def tests_cli_runs_ok(template):
    """Runs the generated CLI's help works"""
    docker_run_devimg([DEFAULT_CONF["project_slug"], "--help"], template.path)


class CasesDockerBuild:
    """Test cases for the docker-building commands"""

    def case_docker_build_dev(self):
        """Build the dev container via make"""
        return (["make", "docker-build-dev"], DEFAULT_CONF["project_slug"] + "-dev")

    def case_docker_build_release(self):
        """Build the release container via make"""
        return (["make", "docker-build-release"], DEFAULT_CONF["project_slug"])


@parametrize_with_cases("make_cmd,img_name", cases=CasesDockerBuild)
def tests_template_makes_docker_ok(template, make_cmd, img_name):
    """Checks we can build a docker image on rendered code"""
    subprocess.check_call(make_cmd, cwd=template.path)
    subprocess.check_call(["docker", "image", "rm", img_name])
