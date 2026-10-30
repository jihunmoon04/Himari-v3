# MCP defaults in Settings

Settings → MCP changes the default availability of each configured MCP source.
Only the authenticated operator can read or change these defaults. The UI does
not expose server addresses, commands, credentials or permission configuration.

- `GET /api/settings/mcp` returns `revision` and `sources` (source name, boolean
  `default`, startup availability and tool count).
- `PUT /api/settings/mcp/{source}` takes a strict boolean `enabled` and the last
  `revision`. A stale revision returns 409; the UI reads back the state rather
  than replaying an uncertain write. Unknown sources return 404.
- Overrides are written atomically, mode 600, under the existing private control
  directory as `mcp-defaults.json`. `mcp.json` remains the server/security
  configuration. Startup applies saved defaults before constructing access checks.
- Once persisted, the running access checks and source views update together.
  Conversations following Default see the change in subsequent model requests
  and tool authorization checks. Existing stored conversation overrides remain.
  As before, choosing the current default in a conversation means following it,
  rather than pinning that value independently of later default changes.

Default availability does not manage process lifetime. Startup still connects
all configured servers to discover their tools. Turning a default off neither
disconnects a shared stdio server nor stops an independently hosted HTTP server.
The existing stdio idle timeout applies; in the deployed Calliope configuration,
the process is shared and the default timeout is 1,800 seconds. A later call can
start its process again. HTTP services manage their own lifetime.

Validation includes live inherited access, preserved conversation overrides,
restart persistence, stale updates, authentication, invalid inputs, refused
writes and private-file/link checks. This adds a Web API and runs full pytest;
the MCP wire protocol and sandbox lifecycle are unchanged.
