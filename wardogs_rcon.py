#!/usr/bin/env python3
"""固定脚本使用的 WARDOGS RCON 查询与地图命令。写操作需 --execute。"""

import argparse
import http.client
import json
from pathlib import Path
import sys


MAPS = {
    "bakurani": ("Kavkazi", "Bakurani_KOTH_01"),
    "ozeti": ("Europe", "Madrid_KOTH_01"),
    "zestafona": ("NorthAmerica", "Detroit_KOTH_01"),
}


def build_request(args):
    if args.command in ("status", "rotation"):
        return "GET", "/v1/" + args.command, None
    if args.command in ("end", "restart"):
        return "POST", "/v1/match/" + args.command, None
    map_id, experience = MAPS[args.map]
    body = {"map": map_id, "experiences": [experience], "lighting": args.lighting}
    if args.zone:
        body["zoneAlternator"] = args.zone
    return "POST", "/v1/match/map", body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=10246)
    parser.add_argument("--key-file", type=Path,
                        default=Path(__file__).resolve().parent / ".secrets" / "rcon_key")
    parser.add_argument("--execute", action="store_true", help="实际执行写操作")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "rotation", "end", "restart"):
        commands.add_parser(name)
    map_parser = commands.add_parser("map", help="普通模式切图，不附加纯步兵或硬核修饰项")
    map_parser.add_argument("map", choices=MAPS)
    map_parser.add_argument("--lighting", default="DayClear")
    map_parser.add_argument("--zone", help="使用对应地图 alternators 目录中的完整 tag")
    args = parser.parse_args()
    method, path, body = build_request(args)
    if method != "GET" and not args.execute:
        print(json.dumps({"dryRun": True, "method": method, "path": path,
                          "body": body}, ensure_ascii=False))
        return 0
    key = ""
    connection = None
    try:
        key = args.key_file.read_text(encoding="utf-8-sig").strip()
        if not key:
            raise ValueError("密码文件为空")
        connection = http.client.HTTPConnection(args.host, args.port, timeout=20)
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        text = response.read(262144).decode("utf-8", errors="replace")
        result = json.loads(text)
        output = json.dumps({"httpStatus": response.status, "response": result},
                            ensure_ascii=False)
        print(output.replace(key, "[REDACTED]"))
        return 0 if 200 <= response.status < 300 and result.get("ok") is not False else 1
    except (OSError, ValueError, http.client.HTTPException) as error:
        message = str(error).replace(key, "[REDACTED]") if key else str(error)
        print(json.dumps({"error": message, "writeOutcomeUnknown": method != "GET"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
