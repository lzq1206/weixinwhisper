from __future__ import annotations

import asyncio
import sys

import wxmoments
import wxmoments_archive


async def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0].lower() == "archive":
        return await wxmoments_archive.main(args[1:])
    return await wxmoments.main(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
