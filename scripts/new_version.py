"""Create a new local resume version."""

import argparse
from pathlib import Path
import sys

try:
    from .resumelib import create_version
except ImportError:
    from resumelib import create_version


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="이력서의 새 버전을 만듭니다.")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--bump", choices=("major", "minor", "patch"))
    choice.add_argument("--version", help="새 버전 번호(X.Y.Z)")
    parser.add_argument("--note", required=True, help="버전 메모")
    parser.add_argument("--content", type=Path, help="본문 HTML 파일")
    parser.add_argument("--docx", type=Path, help="이미 만든 Word 파일")
    parser.add_argument("--date", help="YYYY-MM-DD; 기본값은 오늘")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        content = args.content.read_text(encoding="utf-8") if args.content else None
        result = create_version(args.root, bump=args.bump, version=args.version,
                                note=args.note, content_html=content,
                                docx_path=args.docx, date=args.date)
    except (OSError, ValueError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1
    print(str(args.root.resolve() / result["path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
