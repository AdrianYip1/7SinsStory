import re
import subprocess
import sys
from pathlib import Path


PATTERNS = [
    r"AKIA[0-9A-Z]{16}",  # AWS access key ID
    r"ASIA[0-9A-Z]{16}",  # AWS STS key ID
    r"aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}",
    r"NOTION_TOKEN\s*[:=]\s*['\"]?secret_[A-Za-z0-9]+",
    r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
    r"ghp_[A-Za-z0-9]{36}",  # GitHub PAT classic
    r"github_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PAT
    r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token family
    r"AIza[0-9A-Za-z\-_]{35}",  # Google API key
]


def get_staged_files():
    out = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB"],
        text=True,
    )
    files = [f.strip() for f in out.splitlines() if f.strip()]
    return files


def file_is_text(path: Path):
    try:
        data = path.read_bytes()
    except Exception:
        return False
    if b"\x00" in data:
        return False
    return True


def main():
    try:
        staged = get_staged_files()
    except subprocess.CalledProcessError as e:
        print(f"[secret-check] Unable to read staged files: {e}")
        return 1

    offenders = []
    for f in staged:
        p = Path(f)
        if not p.exists() or p.is_dir():
            continue
        if not file_is_text(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in PATTERNS:
            m = re.search(pat, text)
            if m:
                offenders.append((f, pat, m.group(0)[:120]))
                break

    if offenders:
        print("\n[secret-check] Commit blocked. Possible secret(s) detected in staged files:\n")
        for f, pat, sample in offenders:
            print(f"- {f}")
            print(f"  pattern: {pat}")
            print(f"  sample : {sample}")
        print("\nRemove/redact secrets, then re-stage and commit again.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
