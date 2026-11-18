# Operations · 使用与运维

## Events and local history · 事件及本地历史

Create `data/archive` and `data/local-cache` with the configured UID/GID. Generate separate random 32-byte hex tokens in `.secrets/event_token` (webhook ingest) and `.secrets/event_query_token` (read/export). Use file permissions 0600.

Run `docker compose --profile events up -d --build`. Route the game's webhook over TLS to `POST /wardogs` on the receiver; pass the ingest token as a bearer credential. Expose only that route through the proxy, and keep query and RCON traffic on trusted networks. The cache reads the receiver over the Compose network and writes `data/local-cache/events.sqlite3`. It also reads RCON player, server and chat state. Externally hosted receivers can be configured with an HTTPS `EVENT_BASE_URL` on the cache container. Use `GET /v1/sync-status` with the export token to check source freshness.

启用 events profile 前分别生成回调写入与导出只读令牌。反代只向外提供 `POST /wardogs`，在代理终止 TLS。近时段击杀和聊天等接口要求同步新鲜度，返回 503 时检查接收、RCON 能力、游戏版本、限流和本地缓存，不用旧数据充当实时结论。聊天数据可能含私人内容，按需启用并限制访问。

## Moderation · 管理权限

- Keep RCON, service, report, admin, ingest and export credentials distinct. The base service token can broadcast, so give read-only consumers a proxy that allows only the required routes.
- To enable admin endpoints, create `.secrets/admin_key` and `.secrets/admin_scopes.json`. The latter has the form `{"admin_moderation_scopes":[{"platform":"qq_official:example","botId":"example-bot","groupId":"example-group"}]}`. Replace all examples with the identities actually reported by the adapter. Start with `docker compose -f compose.yaml -f compose.admin.yaml up -d --build`.
- For report execution, create `.secrets/report_key` and add `-f compose.reports.yaml` to the same Compose invocation. Keep report execution disabled while testing routing and evidence review. Enable local restriction storage via `rejoin_guard_enabled` in `service/config.json` only if the corresponding enforcement is intended. Add `--profile events` to the same invocation when those services are needed.
- The admin token belongs in a trusted adapter. That adapter must verify group and actor role, since the backend authorizes the supplied `actor` against the token and configured scope. Use the exact OpenAPI request and response fields.
- The server identity route expects an operator-supplied `data/server-identity.json` with `serverId` and fresh Unix `lastSuccess` fields, and `error` null or absent. The former site's watcher is not bundled. The RCON chat feature requires a server build that exposes chat and any applicable chat terms to be handled by the operator.

管理令牌仅供可信适配器使用。先验证只读状态和角色绑定，再按需启用写入能力。自动策略应在隔离环境用替身玩家测试。迁移时保留幂等记录、待处理状态和封禁审计。

## Diagnostics and backup · 排查与备份

1. Check container logs, `/healthz`, the state API and the cache's freshness before changing anything.
2. Record the current image and config revision. Quiesce writers before taking filesystem backups, or use SQLite's online backup API for each database. Preserve file permissions.
3. Investigate pending or unknown operations by their operation ID. Check receipts and actual state before retrying. Never replay historical commands from a restored database.
4. Use `PYTHONPATH=tests:. python -m unittest discover -s tests` for the included fixture suite.

备份恢复应与权限配置一起核验。不要重放处理中或结果不明的写操作。旧版可能不理解新版本的任务状态，回退需先停写并检查待执行任务。
