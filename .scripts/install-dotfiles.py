#!/usr/bin/env python3
"""Interactive first checkout for the dotfiles .mrconfig catalog."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field


MR_URL = "git@github.com:allpan3/dotfiles-mr.git"
MR_BRANCH = "master"
VCSH_DIR = Path(".config/vcsh/repo.d")
GIT_ENV = {
    key: value for key, value in os.environ.items()
    if key not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX", "GIT_NAMESPACE",
    }
}


class InstallError(Exception):
    pass


@dataclass
class Repo:
    name: str
    dest: Path
    vcsh: bool
    settings: dict = field(default_factory=dict)
    url: str = ""
    branch: str = ""
    after: str = ""
    snapshot: Path | None = None
    installed: bool = False
    new_branch: bool = False
    header_line: int | None = None
    skip_line: int | None = None

    @property
    def ref(self):
        return f"refs/heads/{self.branch}"

    @property
    def skip_rule(self):
        rule = self.settings.get("skip", "false").strip()
        literal = re.fullmatch(r"(false|true|lazy)(?:[ \t]+#[^\r\n]*)?", rule)
        return literal[1] if literal else rule


def run(*args, capture=False, cwd=None, env=None, check=True):
    return subprocess.run(
        args, check=check, cwd=cwd, env=GIT_ENV if env is None else env,
        stdout=subprocess.PIPE if capture else None,
    )


def git_dir(directory, *args, **kwargs):
    return run("git", "--git-dir", directory, *args, **kwargs)


def text(result):
    return os.fsdecode(result.stdout).rstrip("\n")


def catalog_path(home, value):
    for prefix in ("$HOME/", "${HOME}/", "~/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    path = home / value
    if not path.is_relative_to(home) or path == home or ".." in path.parts or "$" in value:
        raise InstallError(f"Unsupported catalog path {value!r}")
    return path


def read_catalog(source, home):
    sections = {}
    headers = {}
    skip_lines = {}
    section = key = None
    for number, line in enumerate(source.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        header = re.fullmatch(r"\[([^\]]+)\]\s*", line)
        setting = re.fullmatch(r"(\w+)\s*=\s*(.*)", line)
        if header:
            section = header[1]
            sections.setdefault(section, {})
            headers[section] = number - 1
            key = None
        elif setting and section is not None:
            key = setting[1]
            sections[section][key] = setting[2]
            if key == "skip":
                skip_lines[section] = number - 1
        elif line[0].isspace() and key is not None:
            sections[section][key] += "\n" + line.strip()
        else:
            raise InstallError(f"Unsupported .mrconfig syntax on line {number}")
        if key in {"include", "chain"}:
            raise InstallError(f"This installer supports a flat .mrconfig. Found {key} on line {number}.")

    defaults = sections.pop("DEFAULT", {})
    sections.pop("ALIAS", None)
    repos = {}
    for header, settings in sections.items():
        dest = catalog_path(home, header)
        vcsh = dest.parent == home / VCSH_DIR and dest.suffix == ".git"
        name = dest.stem if vcsh else dest.name
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name) or name.startswith("-"):
            raise InstallError(f"Unsupported repo name {name!r}")
        if name in repos:
            raise InstallError(f"Multiple catalog entries use the name {name!r}")
        repos[name] = Repo(name, dest, vcsh, {**defaults, **settings},
                           header_line=headers[header], skip_line=skip_lines.get(header))
    return repos


def parse_checkout(repo, home):
    command = repo.settings.get("checkout", "")
    if any(key in repo.settings for key in ("lib", "pre_checkout", "post_checkout", "fixups",
                                            "checkout_append", "checkout_prepend")):
        raise InstallError(f"{repo.name} has custom checkout settings. Review its .mrconfig entry.")
    if repo.skip_rule not in {"false", "lazy"}:
        raise InstallError(f"{repo.name} has a custom skip rule. Review its .mrconfig entry.")
    if repo.settings.get("deleted", "false") != "false":
        raise InstallError(f"{repo.name} has a custom deleted rule. Review its .mrconfig entry.")

    # The local entry in this catalog chooses a branch and handles missing remote branches.
    if repo.name == "local" and repo.vcsh and "\n" in command:
        remote = re.search(r'^remote=(.+)$', command, re.MULTILINE)
        if remote and 'vcsh clone -b "$branch" "$remote" local' in command:
            words = shlex.split(remote[1])
            if len(words) == 1 and "$" not in words[0]:
                repo.url = words[0]
                repo.branch = os.environ.get("DOTFILES_BRANCH", "") or input("Machine branch for local: ").strip()
                check_branch(repo.branch)
                return

    clone, _, repo.after = command.partition(" && ")
    words = shlex.split(clone)

    if words[:2] != ["vcsh" if repo.vcsh else "git", "clone"]:
        raise InstallError(f"{repo.name} needs a git clone or vcsh clone checkout command")
    arguments = words[2:]
    if len(arguments) >= 2 and arguments[0] in {"-b", "--branch"}:
        repo.branch = arguments[1]
        check_branch(repo.branch)
        arguments = arguments[2:]
    if len(arguments) != 2 or arguments[0].startswith("-") or "$" in arguments[0]:
        raise InstallError(f"Unsupported clone arguments in the {repo.name} checkout")
    repo.url, target = arguments
    if (repo.vcsh and target != repo.name) or (
        not repo.vcsh and catalog_path(home, target) != repo.dest
    ):
        raise InstallError(f"The clone target for {repo.name} does not match its .mrconfig section")


def check_branch(branch):
    if not branch or branch.startswith("-") or run(
        "git", "check-ref-format", f"refs/heads/{branch}", check=False
    ).returncode:
        raise InstallError(f"Invalid branch name {branch!r}")


def stage_repo(repo, staging):
    repo.snapshot = staging / f"{repo.name}.git"
    if repo.name == "local" and repo.vcsh:
        result = run("git", "ls-remote", "--exit-code", "--heads", "--",
                     repo.url, repo.ref, capture=True, check=False)
        if result.returncode == 2:
            repo.new_branch = True
            return
        result.check_returncode()

    options = ["--single-branch", "--branch", repo.branch] if repo.branch else []
    run("git", "clone", "--quiet", "--bare", *options, "--", repo.url, repo.snapshot)
    if not repo.branch:
        if repo.vcsh:
            for branch in ("main", "master", "trunk", "development"):
                if git_dir(repo.snapshot, "show-ref", "--verify", "--quiet",
                           f"refs/heads/{branch}", check=False).returncode == 0:
                    repo.branch = branch
                    break
        else:
            repo.branch = text(git_dir(repo.snapshot, "symbolic-ref", "--short", "HEAD", capture=True))
    if not repo.branch or git_dir(repo.snapshot, "show-ref", "--verify", "--quiet",
                                   repo.ref, check=False).returncode:
        raise InstallError(f"No checkout branch found for {repo.name}. Specify its branch in .mrconfig.")


def choose(repos):
    optional = [repo for repo in repos if not repo.installed and repo.name not in {"mr", "dot"}]
    if not optional:
        return set()
    print("\nOptional repositories")
    for index, repo in enumerate(optional, 1):
        print(f"  {index}. {repo.name}")
    by_choice = {str(index): repo.name for index, repo in enumerate(optional, 1)}
    by_choice.update({repo.name: repo.name for repo in optional})
    default = {repo.name for repo in optional if repo.skip_rule == "lazy"}
    default_label = ", ".join(sorted(default)) or "none"
    while True:
        answer = input(
            f"Skip by number or name (spaces or commas, all or none) [{default_label}]: "
        ).strip()
        if not answer:
            return default
        if answer == "none":
            return set()
        if answer == "all":
            return {repo.name for repo in optional}
        choices = answer.replace(",", " ").split()
        unknown = [choice for choice in choices if choice not in by_choice]
        if not unknown:
            return {by_choice[choice] for choice in choices}
        print(f"Unknown selection: {', '.join(unknown)}")


def add_lazy_rules(source, repos):
    lines = source.splitlines(keepends=True)
    for repo in repos:
        if repo.skip_rule != "false" or {"skip_append", "skip_prepend"} & repo.settings.keys():
            raise InstallError(f"{repo.name} has a custom skip rule. Review it before adding skip = lazy.")
        if repo.skip_line is not None:
            index = repo.skip_line
            lines[index] = re.sub(r"^(skip[ \t]*=[ \t]*)false\b", r"\g<1>lazy", lines[index])
        else:
            index = repo.header_line
            line = lines[index]
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            if line.endswith("\n"):
                lines[index] = line + "skip = lazy" + ending
            else:
                lines[index] = line + ending + "skip = lazy"
    return "".join(lines)


def save_lazy_rules(config, reviewed_source, repos):
    target = config.resolve(strict=True)
    if not target.is_relative_to(config.parent) or not target.is_file():
        raise InstallError(f"{config} must point to a regular file in the target home.")
    current = os.fsdecode(target.read_bytes())
    if current.replace("\r\n", "\n") != reviewed_source.replace("\r\n", "\n"):
        raise InstallError(".mrconfig changed after it was read. Run the installer again to review it.")
    updated = add_lazy_rules(current, repos)

    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".mrconfig-", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), stat.S_IMODE(target.stat().st_mode))
            file.write(os.fsencode(updated))
            file.close()
            if target.read_bytes() != os.fsencode(current):
                raise InstallError(".mrconfig changed while saving skips. Run the installer again.")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    print("Saved skip = lazy in .mrconfig for " + ", ".join(repo.name for repo in repos) + ".")


def exists(path):
    return os.path.lexists(path)


def installed_repo(repo):
    metadata = repo.dest if repo.vcsh else repo.dest / ".git"
    if not exists(metadata):
        return False
    args = ["--git-dir", metadata] if repo.vcsh else ["-C", repo.dest]
    if run("git", *args, "rev-parse", "--verify", "--quiet", "HEAD", capture=True, check=False).returncode:
        raise InstallError(f"{repo.dest} is an incomplete checkout. Finish or move it before installing.")
    return True


def tracked(directory, *args):
    output = git_dir(directory, *args, capture=True).stdout
    return [Path(os.fsdecode(path)) for path in output.split(b"\0") if path]


def path_key(path):
    value = str(path)
    # macOS commonly compares both case and normalized Unicode names.
    return unicodedata.normalize("NFD", value).casefold() if sys.platform == "darwin" else value


def parent_keys(path):
    return {path_key(parent) for parent in path.parents}


def scan(home, selected, repos):
    repo_dir = home / VCSH_DIR
    ignore_dir = home / ".gitignore.d"
    protected_dirs = [repo_dir]
    if any(repo.vcsh for repo in selected):
        protected_dirs.append(ignore_dir)
    standalone = [repo.dest for repo in repos if not repo.vcsh]
    pending_standalone = {repo.dest for repo in selected if not repo.vcsh}
    if any(path_key(left) in parent_keys(right) | {path_key(right)}
           for i, left in enumerate(standalone) for j, right in enumerate(standalone) if i != j):
        raise InstallError("Standalone repository destinations overlap.")

    owners = {}
    pending = {}
    for metadata in sorted(repo_dir.glob("*.git")):
        for path in tracked(metadata, "ls-files", "--cached", "-z"):
            owners.setdefault(path_key(home / path), (home / path, metadata.stem))
    existing_paths = [path for path, _ in owners.values()]
    existing_paths.extend(repo.dest for repo in repos if repo.installed and not repo.vcsh)

    for repo in selected:
        if repo.new_branch or not repo.vcsh:
            continue
        for relative in tracked(repo.snapshot, "ls-tree", "--full-tree", "-r", "--name-only", "-z", repo.ref):
            path = home / relative
            key = path_key(path)
            if key in owners:
                other_path, other_repo = owners[key]
                raise InstallError(f"{repo.name} needs {path}, which overlaps {other_path} from {other_repo}.")
            owners[key] = (path, repo.name)
            pending[key] = path

    for key, (path, name) in owners.items():
        parents = parent_keys(path)
        for parent_key in parents:
            if parent_key in owners and (key in pending or parent_key in pending):
                parent, parent_repo = owners[parent_key]
                raise InstallError(f"{name} needs {path}, but {parent_repo} tracks {parent} as a file.")
        for directory in [*standalone, *protected_dirs]:
            if key not in pending and directory not in pending_standalone:
                continue
            directory_key = path_key(directory)
            if (
                key in parent_keys(directory) | {directory_key}
                or (directory != ignore_dir and directory_key in parents)
            ):
                raise InstallError(f"{name} needs {path}, which overlaps repository directory {directory}.")

    blockers = {}
    targets = [(path, False) for path in pending.values()]
    targets.extend((directory, True) for directory in sorted(pending_standalone))
    targets.extend((path, True) for path in protected_dirs)
    for path, wants_dir in targets:
        relative = path.relative_to(home)
        for index in range(1, len(relative.parts) + 1):
            component = home.joinpath(*relative.parts[:index])
            try:
                info = component.lstat()
            except FileNotFoundError:
                break
            leaf = component == path
            if stat.S_ISDIR(info.st_mode):
                if not leaf or component in protected_dirs or (wants_dir and not any(component.iterdir())):
                    continue
            if any(component == directory or component in directory.parents for directory in protected_dirs):
                raise InstallError(f"vcsh needs a real directory at {component}. Resolve it before installing.")
            blockers[component] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
            break

    for path in existing_paths:
        for component in (path, *path.parents):
            if component in blockers:
                raise InstallError(f"Backing up {component} would move files from an installed repository.")
    return {path: info for path, info in sorted(blockers.items())
            if not any(parent in blockers for parent in path.parents)}


def install(repo, home):
    print(f"Installing {repo.name} ({repo.branch})", flush=True)
    if repo.installed or installed_repo(repo):
        raise InstallError(f"{repo.dest} appeared during installation.")
    if not repo.vcsh:
        repo.dest.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--quiet", "--no-hardlinks", "--branch", repo.branch,
            "--", repo.snapshot, repo.dest)
        run("git", "-C", repo.dest, "remote", "set-url", "origin", repo.url)
        run("git", "-C", repo.dest, "config", "--replace-all", "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*")
        return

    storage = repo.dest.parent
    storage.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dotfiles-install-", dir=storage) as temp:
        print(f"  Temporary installation directory: {temp}", flush=True)
        env = {
            **GIT_ENV, "VCSH_BASE": str(home), "VCSH_REPO_D": temp,
            "XDG_CONFIG_HOME": str(home / ".config"), "VCSH_WORKTREE": "absolute",
        }
        run("vcsh", "init", repo.name, cwd=home, env=env)
        metadata = Path(temp) / repo.dest.name
        actual_home = text(git_dir(metadata, "config", "--get", "core.worktree", capture=True))
        if Path(actual_home) != home:
            raise InstallError(f"vcsh configured an unexpected work tree for {repo.name}: {actual_home}")
        git_dir(metadata, "read-tree", "--empty", cwd=home)
        git_dir(metadata, "remote", "add", "origin", repo.url)
        git_dir(metadata, "config", "status.showUntrackedFiles", "no")
        if repo.new_branch:
            git_dir(metadata, "symbolic-ref", "HEAD", repo.ref)
            git_dir(metadata, "commit", "--quiet", "--allow-empty", "-m", "Initialize branch", cwd=home)
        else:
            upstream = f"refs/remotes/origin/{repo.branch}"
            git_dir(metadata, "fetch", "--quiet", "--", repo.snapshot, f"{repo.ref}:{upstream}")
            git_dir(metadata, "checkout", "--quiet", "--no-overwrite-ignore",
                    "--track", "-b", repo.branch, f"origin/{repo.branch}", cwd=home)
        if exists(repo.dest):
            raise InstallError(f"{repo.dest} appeared during installation.")
        metadata.rename(repo.dest)
    print(f"Installed {repo.name} at {repo.dest}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Inspect the selected Git branches, back up blockers, and install dotfiles.",
        epilog="Applying the plan records skipped repos as skip = lazy in .mrconfig.",
    )
    parser.add_argument("--plan", action="store_true", help="show the install plan without changing the target")
    parser.add_argument("--mr-url", default=MR_URL, help="myrepos catalog Git URL")
    parser.add_argument("--mr-branch", default=MR_BRANCH, help="branch containing .mrconfig (default: master)")
    parser.add_argument("--home", type=Path, default=Path.home(), help="existing target directory (default: home)")
    args = parser.parse_args()
    for program in ("git", "vcsh"):
        if shutil.which(program) is None:
            raise InstallError(f"Install {program} first.")
    home = args.home.resolve(strict=True)
    config = home / ".mrconfig"
    repo_dir = home / VCSH_DIR
    if not home.is_dir():
        raise InstallError(f"{home} is not a directory.")
    if any(path.is_symlink() for path in [repo_dir, repo_dir.parent, repo_dir.parent.parent]):
        raise InstallError(f"vcsh needs a real directory at {repo_dir}.")
    check_branch(args.mr_branch)

    with tempfile.TemporaryDirectory(prefix="dotfiles-stage-") as temp:
        staging = Path(temp)
        mr = Repo("mr", repo_dir / "mr.git", True, url=args.mr_url, branch=args.mr_branch)
        mr.installed = installed_repo(mr)
        if mr.installed:
            print("Using the current ~/.mrconfig from the installed mr repository.")
            source = os.fsdecode(config.read_bytes())
        else:
            stage_repo(mr, staging)
            source = os.fsdecode(git_dir(mr.snapshot, "show", f"{mr.ref}:.mrconfig", capture=True).stdout)

        repos = read_catalog(source, home)
        if "mr" in repos and repos["mr"].dest != mr.dest:
            raise InstallError("The mr entry does not use the expected vcsh directory.")
        repos["mr"] = mr
        if "dot" not in repos or not repos["dot"].vcsh:
            raise InstallError("Expected a dot vcsh entry in .mrconfig.")
        for repo in repos.values():
            if repo.name != "mr" and repo.settings.get("deleted", "false") != "true":
                repo.installed = installed_repo(repo)

        candidates = [repo for repo in repos.values()
                      if repo.skip_rule != "true" and repo.settings.get("deleted") != "true"]
        skipped = choose(candidates)
        skip_edits = [repos[name] for name in sorted(skipped) if repos[name].skip_rule != "lazy"]
        if skip_edits:
            add_lazy_rules(source, skip_edits)
        selected = [repo for repo in candidates if not repo.installed and repo.name not in skipped]
        order = {"mr": 0, "dot": 1}
        selected.sort(key=lambda repo: (order.get(repo.name, 2), int(repo.settings.get("order", 10))))
        for repo in selected:
            if repo is not mr:
                parse_checkout(repo, home)
                stage_repo(repo, staging)

        blockers = scan(home, selected, repos.values())
        print(f"\nTarget: {home}")
        print("Install: " + (", ".join(
            f"{repo.name} ({repo.branch}{', new branch' if repo.new_branch else ''})" for repo in selected
        ) or "none"))
        print("Already present: " + (", ".join(repo.name for repo in candidates if repo.installed) or "none"))
        print("Skipped: " + (", ".join(sorted(skipped)) or "none"))
        if skip_edits:
            print("Record skip = lazy in .mrconfig for " + ", ".join(repo.name for repo in skip_edits) + ".")
        if blockers:
            print("Files or directories to back up:")
            for path in blockers:
                print(f"  {str(path.relative_to(home))!r}")
        else:
            print("Files or directories to back up: none")
        if args.plan or not (selected or skip_edits):
            return
        if input("Apply this plan? [y/N] ").strip().lower() not in {"y", "yes"}:
            return

        if blockers != scan(home, selected, repos.values()):
            raise InstallError("The conflict list changed. Run the installer again to review it.")
        if mr.installed and skip_edits:
            save_lazy_rules(config, source, skip_edits)
        if blockers:
            backup = Path(tempfile.mkdtemp(prefix="dotfiles-backup.", dir=home))
            print(f"Backups: {backup}", flush=True)
            for path in blockers:
                destination = backup / path.relative_to(home)
                destination.parent.mkdir(parents=True, exist_ok=True)
                path.rename(destination)
        for repo in selected:
            install(repo, home)
            if repo is mr and skip_edits:
                save_lazy_rules(config, source, skip_edits)
        for repo in selected:
            if repo.after:
                print(f"Post-checkout command for {repo.name}: {repo.after}")
                if repo.name == "ai":
                    print("The AI setup can replace existing skill directories outside the backup plan.")
                if home == Path.home().resolve() and input("Run this command? [y/N] ").strip().lower() in {"y", "yes"}:
                    env = {
                        **GIT_ENV, "MR_REPO": str(repo.dest), "MR_CONFIG": str(home / ".mrconfig"),
                        "MR_ACTION": "checkout",
                    }
                    run("sh", "-ec", repo.after, cwd=repo.dest.parent, env=env)
            if repo.new_branch:
                print("To publish the new machine branch, run " +
                      shlex.join(["vcsh", repo.name, "push", "-u", "origin", repo.branch]))
        print("Installation complete.")


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError, ValueError) as error:
        sys.exit(f"Install stopped: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"Install stopped: {Path(error.cmd[0]).name} exited with status {error.returncode}.")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nInstall cancelled.")
