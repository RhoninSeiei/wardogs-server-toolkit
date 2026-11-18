#!/usr/bin/env python3
"""调用本机管理服务；独立 API 令牌从文件读取，不输出明文。"""
import argparse
import http.client
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18761)
    parser.add_argument('--key-file', type=Path, default=Path(__file__).resolve().parent / '.secrets/service_api_key')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['health', 'state', 'players', 'match', 'openapi']:
        sub.add_parser(name)
    job = sub.add_parser('job')
    job.add_argument('id')
    broadcast = sub.add_parser('broadcast')
    broadcast.add_argument('message')
    broadcast.add_argument('--idempotency-key', required=True)
    broadcast.add_argument('--english', required=True)
    broadcast.add_argument('--japanese', required=True)
    args = parser.parse_args()
    connection = None
    try:
        paths = {'health': '/healthz', 'state': '/v1/state', 'players': '/v1/players',
                 'match': '/v1/match', 'openapi': '/openapi.json', 'broadcast': '/v1/broadcast'}
        path = '/v1/jobs/' + args.id if args.command == 'job' else paths[args.command]
        headers = {'Accept': 'application/json'}
        if args.command != 'health':
            key = args.key_file.read_text().strip()
            headers['Authorization'] = 'Bearer ' + key
        body = None
        if args.command == 'broadcast':
            headers['Content-Type'] = 'application/json'
            headers['Idempotency-Key'] = args.idempotency_key
            body = json.dumps({'message': args.message, 'english': args.english, 'japanese': args.japanese}).encode()
        connection = http.client.HTTPConnection(args.host, args.port, timeout=15)
        connection.request('POST' if body else 'GET', path, body, headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        print(json.dumps({'httpStatus': response.status, 'response': data}, ensure_ascii=False))
        return 0 if 200 <= response.status < 300 else 1
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(json.dumps({'errorType': type(error).__name__}), file=sys.stderr)
        return 2
    finally:
        if connection:
            connection.close()


if __name__ == '__main__':
    sys.exit(main())
