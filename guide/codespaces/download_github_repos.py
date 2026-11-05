#!/usr/bin/env python3
"""목록의 orm222 GitHub 저장소 9개 다운로드. Python 3.9+, Git 필요.

사용: python download_github_repos.py
      python download_github_repos.py --dest ./external_repos
      python download_github_repos.py --full-history
      python download_github_repos.py --dry-run

기본값은 기본 브랜치의 최신 코드만 받는 shallow clone입니다.
전체 커밋 이력과 다른 브랜치도 받으려면 --full-history를 지정하세요.
기존 저장소는 수정하거나 pull하지 않습니다. 실패해도 다음 항목을 계속합니다.
Git 공식 문서: https://git-scm.com/docs/git-clone
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


REPOSITORIES = (
    "333", "44", "skills", "knowledge-work-plugins", "schemathesis",
    "pytest", "ponytail", "superpowers", "ECC",
)


def valid_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("계정/저장소 이름에 경로나 공백을 넣을 수 없습니다.")
    return value


def git_environment() -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def git_value(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path)] + list(arguments),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=True, env=git_environment(),
    )
    return result.stdout.strip()


def clone_repository(
    name: str, url: str, destination: Path,
    timeout: int = 900, full_history: bool = False,
) -> Dict[str, Any]:
    """임시 폴더에서 복제 성공을 확인한 후에만 최종 경로로 이동한다."""
    target = destination / name
    record = {"repository": name, "url": url, "path": str(target)}
    try:
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not (target / ".git").exists():
                raise ValueError("같은 이름의 경로가 있지만 Git 저장소가 아닙니다.")
            remote = git_value(target, "remote", "get-url", "origin")
            if remote.rstrip("/").removesuffix(".git") != url.rstrip("/").removesuffix(".git"):
                raise ValueError("기존 저장소의 origin 주소가 다릅니다: " + remote)
            record.update(status="skipped", commit=git_value(target, "rev-parse", "HEAD"))
            return record

        logs = destination / "download_logs"
        logs.mkdir(exist_ok=True)
        log_path = logs / (name + ".log")
        record["log"] = str(log_path)
        with tempfile.TemporaryDirectory(prefix="download-", dir=str(destination)) as staging:
            checkout = Path(staging) / "repository"
            command = ["git", "clone"]
            if not full_history:
                command += ["--depth", "1"]
            command += ["--", url, str(checkout)]
            with log_path.open("w", encoding="utf-8") as log:
                subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT,
                    timeout=timeout, check=True, env=git_environment(),
                )
            commit = git_value(checkout, "rev-parse", "HEAD")
            if target.exists() or target.is_symlink():
                raise ValueError("다운로드 도중 같은 이름의 경로가 생겼습니다.")
            checkout.rename(target)
            record.update(status="downloaded", commit=commit)
    except subprocess.TimeoutExpired:
        record.update(status="failed", error="명령 제한 시간을 초과했습니다.")
    except subprocess.CalledProcessError as exc:
        record.update(status="failed", error="Git 실행 실패 (종료 코드 %s)" % exc.returncode)
    except (OSError, ValueError) as exc:
        record.update(status="failed", error=str(exc))
    return record


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner", type=valid_name, default="orm222", help="GitHub 계정 (기본: orm222)")
    parser.add_argument("--dest", type=Path, default=Path("github_repos"), help="저장할 폴더")
    parser.add_argument("--repo", action="append", type=valid_name, help="지정 항목만 다운로드, 여러 번 사용 가능")
    parser.add_argument("--timeout", type=int, default=900, help="저장소당 제한 시간(초), 기본 900")
    parser.add_argument("--full-history", action="store_true", help="전체 Git 이력과 다른 브랜치도 다운로드")
    parser.add_argument("--dry-run", action="store_true", help="다운로드할 주소만 출력")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout은 1 이상이어야 합니다.")
    names = list(dict.fromkeys(args.repo or REPOSITORIES))
    destination = args.dest.expanduser().resolve()
    urls = [(name, "https://github.com/%s/%s.git" % (args.owner, name)) for name in names]
    if args.dry_run:
        for name, url in urls:
            print("%s -> %s" % (url, destination / name))
        return 0
    if shutil.which("git") is None:
        print("Git을 먼저 설치하세요: https://git-scm.com/downloads", file=sys.stderr)
        return 2
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    interrupted = False
    try:
        for index, (name, url) in enumerate(urls, 1):
            print("[%d/%d] %s 다운로드 시작" % (index, len(urls), url), flush=True)
            record = clone_repository(name, url, destination, args.timeout, args.full_history)
            records.append(record)
            status = {"downloaded": "완료", "skipped": "기존 저장소: 건너뜀", "failed": "실패"}[record["status"]]
            print("  %s: %s" % (status, record.get("error", record["path"])), flush=True)
            if record["status"] == "failed" and "log" in record:
                print("  오류 로그: " + record["log"], flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\n중단했습니다. 완료된 저장소는 보존됩니다.", file=sys.stderr)
    counts = {status: sum(r["status"] == status for r in records) for status in ("downloaded", "skipped", "failed")}
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "owner": args.owner, "requested": names, "full_history": args.full_history,
        "interrupted": interrupted, "counts": counts, "repositories": records,
    }
    report_path = destination / "download_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n완료 %(downloaded)d / 기존 %(skipped)d / 실패 %(failed)d" % counts)
    print("결과 기록: " + str(report_path))
    return 130 if interrupted else (1 if counts["failed"] else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as error:
        print("파일 처리 실패: " + str(error), file=sys.stderr)
        sys.exit(2)
