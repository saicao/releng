#!/usr/bin/env python3
import argparse
import base64
import dataclasses
from dataclasses import dataclass, field
from enum import Enum
import graphlib
import json
import os
from pathlib import Path, PurePath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Callable, Optional, Mapping, Sequence, Union
import urllib.request

RELENG_DIR = Path(__file__).parent.resolve()
ROOT_DIR = RELENG_DIR.parent

if __name__ == "__main__":
    # TODO: Refactor
    sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(RELENG_DIR / "tomlkit"))

from tomlkit.toml_file import TOMLFile

from releng import env, winenv
from releng.machine_spec import MachineSpec


BUNDLE_URL = "https://build.frida.re/deps/{version}/{filename}"

DEPS_TOML_PATH = RELENG_DIR / "deps.toml"


class Bundle(Enum):
    TOOLCHAIN = 1,
    SDK = 2,


class SourceState(Enum):
    PRISTINE = 1,
    MODIFIED = 2,


class BundleNotFoundError(Exception):
    pass


@dataclass
class OptionSpec:
    value: str
    when: Optional[str] = None


@dataclass
class DependencySpec:
    identifier: str
    when: Optional[str] = None


@dataclass
class PackageSpec:
    identifier: str
    name: str
    version: str
    url: str
    options: list[OptionSpec] = field(default_factory=list)
    dependencies: list[DependencySpec] = field(default_factory=list)
    scope: Optional[str] = None
    when: Optional[str] = None


@dataclass
class DependencyParameters:
    deps_version: str
    bootstrap_version: str
    packages: dict[str, PackageSpec]


class CommandError(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    bundle_opt_kwargs = {
        "help": "Bundle",
        "type": parse_bundle_option_value,
    }
    host_opt_kwargs = {
        "help": "OS/arch",
        "type": MachineSpec.parse,
    }

    command = subparsers.add_parser("sync", help="ensure prebuilt dependencies are up-to-date")
    command.add_argument("bundle", **bundle_opt_kwargs)
    command.add_argument("host", **host_opt_kwargs)
    command.add_argument("location", help="filesystem location", type=Path)
    command.set_defaults(func=lambda args: sync(args.bundle, args.host, args.location.resolve()))

    command = subparsers.add_parser("roll", help="build and upload prebuilt dependencies if needed")
    command.add_argument("bundle", **bundle_opt_kwargs)
    command.add_argument("host", **host_opt_kwargs)
    command.add_argument("--activate", default=False, action='store_true')
    command.add_argument("--post", help="post-processing script")
    command.set_defaults(func=lambda args: roll(args.bundle, args.host, args.activate,
                                                Path(args.post) if args.post is not None else None))

    command = subparsers.add_parser("build", help="build prebuilt dependencies")
    command.add_argument("--bundle", default=Bundle.SDK, **bundle_opt_kwargs)
    command.add_argument("--host", default=MachineSpec.make_from_local_system().identifier, **host_opt_kwargs)
    command.add_argument("--only", help="only build packages A, B, and C", metavar="A,B,C",
                         type=parse_set_option_value)
    command.add_argument("--exclude", help="exclude packages A, B, and C", metavar="A,B,C",
                         type=parse_set_option_value, default=set())
    command.set_defaults(func=lambda args: build(args.bundle, args.host, args.only, args.exclude))

    command = subparsers.add_parser("wait", help="wait for prebuilt dependencies if needed")
    command.add_argument("bundle", **bundle_opt_kwargs)
    command.add_argument("host", **host_opt_kwargs)
    command.set_defaults(func=lambda args: wait(args.bundle, args.host))

    command = subparsers.add_parser("bump", help="bump dependency versions")
    command.set_defaults(func=lambda args: bump())

    args = parser.parse_args()
    if 'func' in args:
        try:
            args.func(args)
        except CommandError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_usage(file=sys.stderr)
        sys.exit(1)


def parse_bundle_option_value(raw_bundle: str) -> Bundle:
    try:
        return Bundle[raw_bundle.upper()]
    except KeyError:
        choices = "', '".join([e.name.lower() for e in Bundle])
        raise argparse.ArgumentTypeError(f"invalid choice: {raw_bundle} (choose from '{choices}')")


def parse_set_option_value(v: str) -> set[str]:
    return set([v.strip() for v in v.split(",")])


def sync(bundle: Bundle,
         machine: MachineSpec,
         location: Path,
         version: Optional[str] = None) -> SourceState:
    state = SourceState.PRISTINE

    if version is None:
        version = load_dependency_parameters().deps_version

    bundle_nick = bundle.name.lower() if bundle != Bundle.SDK else bundle.name

    if location.exists():
        try:
            cached_version = (location / "VERSION.txt").read_text(encoding="utf-8").strip()
            if cached_version == version:
                return state
        except:
            pass
        shutil.rmtree(location)
        state = SourceState.MODIFIED

    (url, filename) = compute_bundle_parameters(bundle, machine, version)

    local_bundle = location.parent / filename
    if local_bundle.exists():
        print("Deploying local {}...".format(bundle_nick), flush=True)
        archive_path = local_bundle
        archive_is_temporary = False
    else:
        if bundle == Bundle.SDK:
            print(f"Downloading SDK {version} for {machine.identifier}...", flush=True)
        else:
            print(f"Downloading {bundle_nick} {version}...", flush=True)
        try:
            with urllib.request.urlopen(url) as response, \
                    tempfile.NamedTemporaryFile(delete=False) as archive:
                shutil.copyfileobj(response, archive)
                archive_path = Path(archive.name)
                archive_is_temporary = True
            print(f"Extracting {bundle_nick}...", flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BundleNotFoundError(f"missing bundle at {url}") from e
            raise e

    try:
        staging_dir = location.parent / f"_{location.name}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)

        with tarfile.open(archive_path, "r:bz2") as tar:
            tar.extractall(staging_dir)

        suffix_len = len(".frida.in")
        raw_location = location.as_posix()
        for f in staging_dir.rglob("*.frida.in"):
            target = f.parent / f.name[:-suffix_len]
            f.write_text(f.read_text(encoding="utf-8").replace("@FRIDA_TOOLROOT@", raw_location),
                         encoding="utf-8")
            f.rename(target)

        staging_dir.rename(location)
    finally:
        if archive_is_temporary:
            archive_path.unlink()

    return state


def roll(bundle: Bundle, machine: MachineSpec, activate: bool, post: Optional[Path]):
    params = load_dependency_parameters()
    version = params.deps_version

    if activate and bundle == Bundle.SDK:
        configure_bootstrap_version(version)

    (public_url, filename) = compute_bundle_parameters(bundle, machine, version)

    # First do a quick check to avoid hitting S3 in most cases.
    request = urllib.request.Request(public_url)
    request.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(request) as r:
            return
    except urllib.request.HTTPError as e:
        if e.code != 404:
            raise CommandError("network error") from e

    s3_url = "s3://build.frida.re/deps/{version}/{filename}".format(version=version, filename=filename)

    # We will most likely need to build, but let's check S3 to be certain.
    r = subprocess.run(["aws", "s3", "ls", s3_url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
    if r.returncode == 0:
        return
    if r.returncode != 1:
        raise CommandError(f"unable to access S3: {r.stdout.strip()}")

    artifact = build(bundle, machine)

    if post is not None:
        post_script = RELENG_DIR / post
        if not post_script.exists():
            raise CommandError("post-processing script not found")

        subprocess.run([
                           sys.executable, post_script,
                           "--bundle=" + bundle.name.lower(),
                           "--host=" + machine.identifier,
                           "--artifact=" + str(artifact),
                           "--version=" + version,
                       ],
                       check=True)

    subprocess.run(["aws", "s3", "cp", artifact, s3_url], check=True)

    # Use the shell for Windows compatibility, where npm generates a .bat script.
    subprocess.run("cfcli purge " + public_url, shell=True, check=True)

    if activate and bundle == Bundle.TOOLCHAIN:
        configure_bootstrap_version(version)


def build(bundle: Bundle,
          machine: MachineSpec,
          only_packages: Optional[set[str]] = None,
          excluded_packages: set[str] = set()) -> Path:
    builder = Builder(bundle, machine)
    try:
        return builder.build(only_packages, excluded_packages)
    except subprocess.CalledProcessError as e:
        print(e, file=sys.stderr)
        if e.stdout is not None:
            print("\n=== stdout ===\n" + e.stdout, file=sys.stderr)
        if e.stderr is not None:
            print("\n=== stderr ===\n" + e.stderr, file=sys.stderr)
        sys.exit(1)


class Builder:
    def __init__(self, bundle: Bundle, host_machine: MachineSpec):
        self._bundle = bundle
        self._build_machine = MachineSpec.make_from_local_system()
        self._host_machine = host_machine
        self._default_library = "static"
        runtimes = ["static"]
        if host_machine.os == "windows" and bundle is Bundle.SDK:
            runtimes += ["dynamic"]
        self._runtimes = runtimes

        self._params = load_dependency_parameters()
        self._cachedir = detect_cache_dir(ROOT_DIR)
        self._workdir = self._cachedir / "src"

        self._toolchain_prefix: Optional[Path] = None
        self._native_file: Optional[Path] = None
        self._cross_file: Optional[Path] = None
        self._machine_env: dict[str, str] = {}

        self._ansi_supported = os.environ.get("TERM") != "dumb" \
                    and (self._build_machine.os != "windows" or "WT_SESSION" in os.environ)

    def build(self,
              only_packages: Optional[list[str]],
              excluded_packages: set[str]) -> Path:
        started_at = time.time()
        prepare_ended_at = None
        build_ended_at = None
        packaging_ended_at = None
        try:
            all_packages = {i: self._resolve_package(p) for i, p in self._params.packages.items() \
                    if self._can_build(p)}
            if only_packages is not None:
                toplevel_packages = [all_packages[identifier] for identifier in only_packages]
                selected_packages = self._resolve_dependencies(toplevel_packages, all_packages)
            elif self._bundle is Bundle.TOOLCHAIN:
                toplevel_packages = [p for p in all_packages.values() if p.scope == "toolchain"]
                selected_packages = self._resolve_dependencies(toplevel_packages, all_packages)
            else:
                selected_packages = {i: p for i, p, in all_packages.items() if p.scope is None}
            selected_packages = {i: p for i, p in selected_packages.items() if i not in excluded_packages}

            ts = graphlib.TopologicalSorter({pkg.identifier: {dep.identifier for dep in pkg.dependencies} \
                    for pkg in selected_packages.values()})
            packages = [selected_packages[identifier] for identifier in ts.static_order()]

            self._prepare(packages)
            prepare_ended_at = time.time()

            for pkg in packages:
                self._build_package(pkg)
            build_ended_at = time.time()

            artifact_file = self._package()
            packaging_ended_at = time.time()
        finally:
            ended_at = time.time()

            if prepare_ended_at is not None:
                print("")
                print("# Time spent")
                print("")
                print("      Total: {}".format(format_duration(ended_at - started_at)))

            if prepare_ended_at is not None:
                print("    Prepare: {}".format(format_duration(prepare_ended_at - started_at)))

            if build_ended_at is not None:
                print("      Build: {}".format(format_duration(build_ended_at - prepare_ended_at)))

            if packaging_ended_at is not None:
                print("  Packaging: {}".format(format_duration(packaging_ended_at - build_ended_at)))

            print("", flush=True)

        return artifact_file

    def _can_build(self, pkg: PackageSpec) -> bool:
        return self._evaluate_condition(pkg.when)

    def _resolve_package(self, pkg: PackageSpec) -> bool:
        resolved_opts = [opt for opt in pkg.options if self._evaluate_condition(opt.when)]
        resolved_deps = [dep for dep in pkg.dependencies if self._evaluate_condition(dep.when)]
        return dataclasses.replace(pkg,
                                   options=resolved_opts,
                                   dependencies=resolved_deps)

    def _resolve_dependencies(self,
                              packages: Sequence[PackageSpec],
                              all_packages: Mapping[str, PackageSpec]) -> dict[str, PackageSpec]:
        result = {p.identifier: p for p in packages}
        for p in packages:
            self._resolve_package_dependencies(p, all_packages, result)
        return result

    def _resolve_package_dependencies(self,
                                      package: PackageSpec,
                                      all_packages: Mapping[str, PackageSpec],
                                      resolved_packages: Mapping[str, PackageSpec]):
        for dep in package.dependencies:
            identifier = dep.identifier
            if identifier in resolved_packages:
                continue
            p = all_packages[identifier]
            resolved_packages[identifier] = p
            self._resolve_package_dependencies(p, all_packages, resolved_packages)

    def _evaluate_condition(self, cond: Optional[str]) -> bool:
        if cond is None:
            return True
        global_vars = {
            "Bundle": Bundle,
            "bundle": self._bundle,
            "machine": self._host_machine,
        }
        return eval(cond, global_vars)

    def _prepare(self, packages: list[PackageSpec]):
        for pkg in packages:
            state = self._grab_and_prepare(pkg)
            if state == SourceState.MODIFIED:
                self._wipe_build_state()

        self._toolchain_prefix, toolchain_state = ensure_toolchain(self._build_machine,
                                                                   self._cachedir,
                                                                   version=self._params.bootstrap_version)
        if toolchain_state == SourceState.MODIFIED:
            self._wipe_build_state()

        (self._native_file, self._cross_file, machine_paths, machine_env) = \
                env.generate_machine_files(build_machine=self._build_machine,
                                           build_sdk_prefix=None,
                                           host_machine=self._host_machine,
                                           host_sdk_prefix=None,
                                           toolchain_prefix=self._toolchain_prefix,
                                           default_library=self._default_library,
                                           call_selected_meson=self._call_meson,
                                           outdir=self._get_builddir_container())
        menv = {**os.environ, **machine_env}
        menv["PATH"] = os.pathsep.join([str(p) for p in machine_paths]) + os.pathsep + menv["PATH"]
        self._machine_env = menv

    def _grab_and_prepare(self, pkg: PackageSpec) -> SourceState:
        sourcedir = self._get_sourcedir(pkg)
        if sourcedir.exists():
            if query_git_head(sourcedir) == pkg.version:
                state = SourceState.PRISTINE
            else:
                self._print_package_banner(pkg)
                subprocess.run(["git", "fetch", "-q"],
                               cwd=sourcedir,
                               check=True)
                subprocess.run(["git", "checkout", "-q", pkg.version],
                               cwd=sourcedir,
                               check=True)
                state = SourceState.MODIFIED
        else:
            self._print_package_banner(pkg)
            sourcedir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--recurse-submodules", pkg.url, sourcedir.name],
                           cwd=sourcedir.parent,
                           check=True)
            subprocess.run(["git", "checkout", "-q", pkg.version],
                           cwd=sourcedir,
                           check=True)
            state = SourceState.PRISTINE

        return state

    def _wipe_build_state(self):
        for path in (self._get_outdir(), self._get_builddir_container()):
            if path.exists():
                self._print_status(path.relative_to(self._workdir).as_posix(), "Wiping")
                shutil.rmtree(path)

    def _build_package(self, pkg: PackageSpec):
        for runtime in self._runtimes:
            manifest_path = self._get_manifest_path(pkg, runtime)
            if manifest_path.exists():
                continue

            self._print_status(pkg.name, f"Building for the {runtime} CRT" if len(self._runtimes) > 1 else "Building")
            self._build_package_for_runtime(pkg, runtime)

            assert manifest_path.exists()

    def _build_package_for_runtime(self, pkg: PackageSpec, runtime: str):
        sourcedir = self._get_sourcedir(pkg)
        builddir = self._get_builddir(pkg, runtime)

        prefix = self._get_prefix(runtime)
        libdir = prefix / "lib"
        pcdir = prefix / self._host_machine.libdatadir / "pkgconfig"
        if self._host_machine.config != "debug":
            optimization = "s"
            ndebug = "true"
        else:
            optimization = "0"
            ndebug = "false"

        if builddir.exists():
            shutil.rmtree(builddir)

        machine_file_opts = [f"--native-file={self._native_file}"]
        if self._cross_file is not None:
            machine_file_opts += [f"--cross-file={self._cross_file}"]

        self._call_meson([
                             "setup",
                             builddir,
                             *machine_file_opts,
                             f"-Dprefix={prefix}",
                             f"-Dlibdir={libdir}",
                             f"-Dpkg_config_path={pcdir}",
                             f"-Ddefault_library={self._default_library}",
                             f"-Dbackend=ninja",
                             f"-Doptimization={optimization}",
                             f"-Db_ndebug={ndebug}",
                             f"-Dstrip=true",
                             f"-Db_vscrt={vscrt_from_configuration_and_runtime(self._host_machine.config, runtime)}",
                             *[opt.value for opt in pkg.options],
                         ],
                         cwd=sourcedir,
                         env=self._machine_env,
                         capture_output=True,
                         encoding="utf-8",
                         check=True)

        self._call_meson(["install"],
                         cwd=builddir,
                         env=self._machine_env,
                         capture_output=True,
                         encoding="utf-8",
                         check=True)

        manifest_lines = []
        install_locations = json.loads(self._call_meson(["introspect", "--installed"],
                                                        cwd=builddir,
                                                        capture_output=True,
                                                        encoding="utf-8",
                                                        env=self._machine_env).stdout)
        for installed_path in install_locations.values():
            manifest_lines.append(Path(installed_path).relative_to(prefix).as_posix())
        manifest_lines.sort()
        manifest_path = self._get_manifest_path(pkg, runtime)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    def _print_package_banner(self, pkg: PackageSpec):
        if self._ansi_supported:
            print("\n".join([
                "",
                "╭────",
                f"│ 📦 \033[1m{pkg.name}\033[0m",
                "├───────────────────────────────────────────────╮",
                f"│ URL: {pkg.url}",
                f"│ CID: {pkg.version}",
                "├───────────────────────────────────────────────╯",
            ]), flush=True)
        else:
            print("\n".join([
                "",
                f"# {pkg.name}",
                f"- URL: {pkg.url}",
                f"- CID: {pkg.version}",
            ]), flush=True)

    def _print_status(self, scope: str, *args):
        status = " ".join([str(arg) for arg in args])
        if self._ansi_supported:
            print(f"│ \033[1m{scope}\033[0m :: {status}", flush=True)
        else:
            print(f"# {scope} :: {status}", flush=True)

    def _call_meson(self, argv, *args, **kwargs):
        return env.call_meson(argv, use_submodule=True, *args, **kwargs)

    def _package(self):
        outfile = self._cachedir / f"{self._bundle.name.lower()}-{self._host_machine.identifier}.tar.bz2"

        with tempfile.TemporaryDirectory(prefix="frida-deps") as raw_tempdir:
            tempdir = Path(raw_tempdir)

            self._print_status(outfile.name, "Staging files")
            if self._bundle is Bundle.TOOLCHAIN:
                self._stage_toolchain_files(tempdir)
            else:
                self._stage_sdk_files(tempdir)

            self._adjust_manifests(tempdir)
            self._adjust_files_containing_hardcoded_paths(tempdir)

            (tempdir / "VERSION.txt").write_text(self._params.deps_version + "\n", encoding="utf-8")

            self._print_status(outfile.name, "Assembling")
            with tarfile.open(outfile, "w:bz2") as tar:
                tar.add(tempdir, ".")

            self._print_status(outfile.name, "All done")

        return outfile

    def _stage_toolchain_files(self, location: Path) -> list[Path]:
        if self._host_machine.os == "windows":
            mixin_files = []
            for dirpath, dirnames, filenames in os.walk(self._toolchain_prefix):
                relpath = PurePath(dirpath).relative_to(self._toolchain_prefix)
                all_files = [relpath / f for f in filenames]
                mixin_files += [f for f in all_files if not (self._file_is_vala_toolchain_related(f) or \
                        f.parent.name == "manifest")]
            copy_files(self._toolchain_prefix, mixin_files, location)

        files = []
        prefix = self._get_prefix("static")
        for dirpath, dirnames, filenames in os.walk(prefix):
            relpath = PurePath(dirpath).relative_to(prefix)
            all_files = [relpath / f for f in filenames]
            files += [f for f in all_files \
                      if self._file_is_vala_toolchain_related(f) \
                          or (f.parts[0] == "bin" \
                              and f.stem not in {"gdbus", "gio", "gobject-query", "gsettings"} \
                              and not f.stem.startswith("gspawn-") \
                              and f.suffix != ".pdb") \
                          or f.parts[0] == "manifest"]
        copy_files(prefix, files, location)

    def _stage_sdk_files(self, location: Path) -> list[Path]:
        files = []
        outdir = self._get_outdir()
        for runtime in self._runtimes:
            prefix = self._get_prefix("static")
            for dirpath, dirnames, filenames in os.walk(prefix):
                relpath = PurePath(dirpath).relative_to(outdir)
                all_files = [relpath / f for f in filenames]
                files += [f for f in all_files if self._file_is_sdk_related(f)]
            files += [f.relative_to(outdir) for f in \
                    (prefix.parent / (prefix.name[:-7] + "-dynamic") / "lib").glob("**/*.a")]
        copy_files(outdir, files, location, self._transform_sdk_dest)

    def _adjust_files_containing_hardcoded_paths(self, bundledir: Path):
        prefixes = [str(self._get_prefix(runtime)) for runtime in self._runtimes]
        for raw_dirpath, dirnames, filenames in os.walk(bundledir):
            dirpath = Path(raw_dirpath)
            for filename in filenames:
                filepath = dirpath / filename

                if filepath.is_symlink():
                    continue

                try:
                    text = filepath.read_text(encoding="utf-8")

                    new_text = text
                    is_pcfile = filepath.suffix == ".pc"
                    replacement = "${frida_sdk_prefix}" if is_pcfile else "@FRIDA_TOOLROOT@"
                    for prefix in prefixes:
                        new_text = new_text.replace(prefix, replacement)

                    if new_text != text:
                        filepath.write_text(new_text, encoding="utf-8")
                        if not is_pcfile:
                            filepath.rename(dirpath / f"{filename}.frida.in")
                except UnicodeDecodeError:
                    pass

    @staticmethod
    def _adjust_manifests(bundledir: Path):
        for manifest_path in (bundledir / "manifest").glob("*.pkg"):
            lines = []

            prefix = manifest_path.parent.parent
            for entry in manifest_path.read_text(encoding="utf-8").strip().split("\n"):
                if prefix.joinpath(entry).exists():
                    lines.append(entry)

                if entry.startswith("lib/") and entry.endswith(".a"):
                    dynamic_entry = "lib-dynamic/" + entry[4:]
                    if prefix.joinpath(dynamic_entry).exists():
                        lines.append(dynamic_entry)

            if lines:
                lines.sort()
                manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                manifest_path.unlink()

    def _file_is_vala_toolchain_related(self, candidate: PurePath) -> bool:
        if candidate.suffix in {".vapi", ".deps"}:
            return True
        return candidate.name.startswith("valac-") and candidate.suffix == self._host_machine.executable_suffix

    def _file_is_sdk_related(self, candidate: PurePath) -> bool:
        suffix = candidate.suffix
        if suffix == ".pdb":
            return False
        if suffix in [".vapi", ".deps"]:
            return True

        parts = candidate.parts

        if parts[1] == "bin":
            if self._host_machine.config == "debug":
                return False
            if parts[0].endswith("-dynamic"):
                return False
            return candidate.name.startswith("v8-mksnapshot-")

        return "share" not in parts

    @staticmethod
    def _transform_sdk_dest(srcfile: PurePath) -> PurePath:
        parts = srcfile.parent.parts
        rootdir = parts[0]
        subpath = PurePath(*parts[1:])

        if rootdir.endswith("-dynamic") and subpath.parts[0] == "lib":
            subpath = PurePath("lib-dynamic").joinpath(*subpath.parts[1:])

        return subpath / srcfile.name

    def _get_outdir(self) -> Path:
        return self._workdir / f"_{self._bundle.name.lower()}.out"

    def _get_sourcedir(self, pkg: PackageSpec) -> Path:
        return self._workdir / pkg.identifier

    def _get_builddir(self, pkg: PackageSpec, runtime: str) -> Path:
        return self._get_builddir_container() / self._compute_output_id(runtime) / pkg.identifier

    def _get_builddir_container(self) -> Path:
        return self._workdir / f"_{self._bundle.name.lower()}.tmp"

    def _get_prefix(self, runtime: str) -> Path:
        return self._get_outdir() / self._compute_output_id(runtime)

    def _compute_output_id(self, runtime: str) -> str:
        parts = [self._host_machine.identifier]
        if self._host_machine.os == "windows":
            parts += [runtime]
        return "-".join(parts)

    def _get_manifest_path(self, pkg: PackageSpec, runtime: str) -> Path:
        return self._get_prefix(runtime) / "manifest" / f"{pkg.identifier}.pkg"


def vscrt_from_configuration_and_runtime(config: str, runtime: str) -> str:
    result = "md" if runtime == "dynamic" else "mt"
    if config == "debug":
        result += "d"
    return result


def wait(bundle: Bundle, machine: MachineSpec):
    params = load_dependency_parameters()
    (url, filename) = compute_bundle_parameters(bundle, machine, params.deps_version)

    request = urllib.request.Request(url)
    request.get_method = lambda: "HEAD"
    started_at = time.time()
    while True:
        try:
            with urllib.request.urlopen(request) as r:
                return
        except urllib.request.HTTPError as e:
            if e.code != 404:
                return
        print("Waiting for: {}  Elapsed: {}  Retrying in 5 minutes...".format(url, int(time.time() - started_at)), flush=True)
        time.sleep(5 * 60)


def bump():
    params = load_dependency_parameters()

    auth_blob = base64.b64encode(":".join([
                                              os.environ["GH_USERNAME"],
                                              os.environ["GH_TOKEN"]
                                          ]).encode("utf-8")).decode("utf-8")
    auth_header = "Basic " + auth_blob

    for identifier, pkg in params.packages.items():
        url = pkg.url
        if not url.startswith("https://github.com/frida/"):
            continue

        print(f"*** Checking {pkg.name}")

        repo_name = url.split("/")[-1][:-4]
        branch_name = "next" if repo_name == "capstone" else "main"

        url = f"https://api.github.com/repos/frida/{repo_name}/commits/main"
        request = urllib.request.Request(url)
        request.add_header("Authorization", auth_header)
        with urllib.request.urlopen(request) as r:
            response = json.load(r)

        latest = response['sha']
        if pkg.version == latest:
            print(f"\tup-to-date")
        else:
            print(f"\toutdated")
            print(f"\t\tcurrent: {pkg.version}")
            print(f"\t\t latest: {latest}")

            f = TOMLFile(DEPS_TOML_PATH)
            config = f.read()
            config[identifier]["version"] = latest
            f.write(config)

            subprocess.run(["git", "add", "deps.toml"],
                           cwd=RELENG_DIR,
                           check=True)
            subprocess.run(["git", "commit", "-m" f"deps: Bump {pkg.name} to {latest[:7]}"],
                           cwd=RELENG_DIR,
                           check=True)

        print("")


def compute_bundle_parameters(bundle: Bundle,
                              machine: MachineSpec,
                              version: str) -> tuple[str, str]:
    if bundle == Bundle.TOOLCHAIN and machine.os == "windows":
        os_arch_config = "windows-x86"
    else:
        os_arch_config = machine.identifier
    filename = f"{bundle.name.lower()}-{os_arch_config}.tar.bz2"
    url = BUNDLE_URL.format(version=version, filename=filename)
    return (url, filename)


def load_dependency_parameters() -> DependencyParameters:
    config = TOMLFile(DEPS_TOML_PATH).read()

    packages = {}
    for identifier, pkg in config.items():
        if identifier == "dependencies":
            continue
        packages[identifier] = PackageSpec(identifier,
                                           pkg["name"],
                                           pkg["version"],
                                           pkg["url"],
                                           list(map(parse_option, pkg.get("options", []))),
                                           list(map(parse_dependency, pkg.get("dependencies", []))),
                                           pkg.get("scope"),
                                           pkg.get("when"))

    p = config["dependencies"]
    return DependencyParameters(p["version"], p["bootstrap_version"], packages)


def configure_bootstrap_version(version: str):
    f = TOMLFile(DEPS_TOML_PATH)
    config = f.read()
    config["dependencies"]["bootstrap_version"] = version
    f.write(config)


def parse_option(v: Union[str, dict]) -> OptionSpec:
    if isinstance(v, str):
        return OptionSpec(v)
    return OptionSpec(v["value"], v.get("when"))


def parse_dependency(v: Union[str, dict]) -> OptionSpec:
    if isinstance(v, str):
        return DependencySpec(v)
    return DependencySpec(v["id"], v.get("when"))


def query_git_head(repo_path: str) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_path, encoding="utf-8").strip()


def copy_files(fromdir: Path,
               files: list[PurePath],
               todir: Path,
               transformdest: Callable[[PurePath], PurePath] = lambda x: x):
    for filename in files:
        src = fromdir / filename
        dst = todir / transformdest(filename)
        dstdir = dst.parent
        dstdir.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst, follow_symlinks=False)


def format_duration(duration_in_seconds: float) -> str:
    hours, remainder = divmod(duration_in_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))


if __name__ == "__main__":
    main()
