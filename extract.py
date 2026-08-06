#!/usr/bin/env python3
"""Main entry point: extract Douyin chat data via web version."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


def _parse_args():
    """Parse CLI arguments."""
    args = {
        "mode": "extract",
        "name_filter": None,
        "incremental": "--incremental" in sys.argv,
        "download_images": "--download-images" in sys.argv,
        "output_format": "jsonl",
        "output_path": None,
    }

    if "--discover" in sys.argv:
        args["mode"] = "discover"
    elif "--list-conversations" in sys.argv:
        args["mode"] = "list_conversations"
    elif "--export" in sys.argv:
        args["mode"] = "export"

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--filter" and i < len(sys.argv) - 1:
            args["name_filter"] = sys.argv[i + 1]
        elif arg == "--format" and i < len(sys.argv) - 1:
            args["output_format"] = sys.argv[i + 1]
        elif arg == "--output" and i < len(sys.argv) - 1:
            args["output_path"] = sys.argv[i + 1]

    return args


def run_export(args):
    """Export chat data to ChatLab format (no browser needed)."""
    from common import paths
    from extractor.exporter import ChatLabExporter

    fmt = args["output_format"]
    ext = ".json" if fmt == "json" else ".jsonl"
    output_path = args["output_path"] or os.path.join(paths.DATA_DIR, f"export{ext}")

    exporter = ChatLabExporter(
        conv_name=args["name_filter"],
        output_format=fmt,
    )
    exporter.export(output_path)


async def run():
    args = _parse_args()

    # Export mode: no browser needed
    if args["mode"] == "export":
        run_export(args)
        return 0

    from extractor.web_scraper import WebChatScraper

    scraper = WebChatScraper(
        discovery_mode=(args["mode"] == "discover"),
        name_filter=args["name_filter"],
        incremental=args["incremental"],
        download_images=args["download_images"],
    )

    try:
        await scraper.launch()
        if os.environ.get("DOUYIN_CHAT_REQUIRE_EXISTING_LOGIN") == "1":
            # Desktop workers must never wait for an interactive login: the
            # headed login window only exists in the control panel process.
            logged_in = await scraper.has_login()
            if not logged_in:
                print("[-] 未检测到有效登录态（DOUYIN_CHAT_REQUIRE_EXISTING_LOGIN=1），快速退出")
        else:
            logged_in = await scraper.wait_for_login()
        if not logged_in:
            print("[-] 未能登录，退出")
            return 2  # non-zero exit so the panel surfaces this as a failure

        if args["mode"] == "discover":
            duration = 60
            for arg in sys.argv[1:]:
                if arg.isdigit():
                    duration = int(arg)
            await scraper.run_discovery(duration=duration)
        elif args["mode"] == "list_conversations":
            from common import paths
            convs = await scraper.list_conversations()
            out_path = paths.CONVERSATIONS_LIST
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            import json as _json
            import time as _time
            payload = {
                "discovered_at": int(_time.time()),
                "items": [
                    {
                        "nickname": c.get("nickname", ""),
                        "name": c.get("name", ""),
                        "time": c.get("time", ""),
                        "preview": c.get("preview", ""),
                    }
                    for c in convs
                ],
            }
            with open(out_path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[+] 会话列表已写入 {out_path}")
        else:
            await scraper.extract_all()

        return 0

    except KeyboardInterrupt:
        print("\n[*] 用户中断")
        return 130
    except Exception as e:
        print(f"\n[-] 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await scraper.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()) or 0)
