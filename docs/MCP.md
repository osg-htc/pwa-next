# pwa-next for AI assistants (MCP)

`pwa mcp` lets an AI assistant that speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(Claude Code, Claude Desktop and others) look at a pwa-next configuration and, if you allow it, change it.
You can then ask things like:

- "Which hosts in the USATLAS mesh have stopped registering, and since when?"
- "What is tested between psb02-gva.cern.ch and t1-ps01-bw.gridpp.rl.ac.uk?"
- "Which host pairs are traced by more than one mesh with the same settings?"
- "Add ps-lat.example.org and ps-bw.example.org to the WLCG LHCb Latency Group."
- "Disable the TestPing task in testnewtests."

The assistant uses the same permissions, checks and publishing as the web pages. Every change it makes is
recorded under History as "*name* (via MCP)" and published at once.

This is the first stage of [issue #1](https://github.com/osg-htc/pwa-next/issues/1): a local server for
people who can already log in to the pwa-next host. Remote access for everyone else is planned below.

## Running it

```
pwa mcp --as <login> [--allow-changes]
```

- `--as`: the pwa-next account to act as, given by its login (the CILogon subject such as
  `http://cilogon.org/serverA/users/12345`, or a password-file name) or by its e-mail address. The account
  must have signed in to the web pages at least once, and must not be disabled.
- `--allow-changes`: also offer the tools that change things. Without it the assistant can only look.

The server speaks JSON-RPC over standard input and output (MCP's stdio transport, protocol versions
2025-06-18, 2025-03-26 and 2024-11-05). It must run as the account that runs the web application (`pwa` in
the RPM), with the same settings file, so that it reaches the database and can publish.

### From Claude Code

On a machine that can SSH to the pwa-next host as root:

```
claude mcp add pwa-next -- ssh -T root@psconfig-itb.example.org \
  runuser -u pwa -- env PWA_CONFIG=/etc/pwa/pwa.env pwa mcp --as smckee
```

Add `--allow-changes` at the end when you want the assistant to be able to change things.

### From Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pwa-next": {
      "command": "ssh",
      "args": ["-T", "root@psconfig-itb.example.org", "runuser", "-u", "pwa", "--", "env",
               "PWA_CONFIG=/etc/pwa/pwa.env", "pwa", "mcp", "--as", "smckee"]
    }
  }
}
```

## Who can use it

`--as` is not a login: whoever can run `pwa mcp` on the host can act as any account. So this local server
is for the people who already administer the host (root or the `pwa` account). Everyone else waits for the
remote server, which will check who is asking.

Within that, the account named by `--as` decides what can change: a contributor on a host group can add and
remove its hosts and nothing else, an editor of a mesh can enable and disable its tasks, and so on, exactly
as on the web pages. Looking is open to every account, as on the web pages.

## Tools

Looking (always offered):

| Tool | What it answers |
| --- | --- |
| `overview` | Counts, tasks withheld from publication and why, hosts gone quiet per mesh, esmond archives in use |
| `find_hosts` | Hosts matching words in their name, site or display name; optionally those offering a test type first |
| `host_info` | One host: Lookup Service data, groups, every task and mesh it is in |
| `list_meshes`, `mesh_info` | Meshes; one mesh's tasks, archives, withheld tasks, who can change it |
| `list_groups`, `group_info` | Host groups; one group's rule, included groups, effective hosts and how each got there, users |
| `list_tests`, `test_info` | Tests; one test's options, problems pScheduler would reject, the tasks using it |
| `test_type_help` | What a pScheduler test type measures, its tools and options |
| `report` | The Reports page: `gone_quiet`, `duplicate_measurements`, `busiest_hosts`, `capability_gaps`, `versions`, `meshes` |
| `tested_between` | What is measured between two hosts, or everything one host takes part in |
| `published_config` | The pSConfig JSON of a mesh or of a host's feed (large ones summarised unless `full` is set) |
| `history` | Recent changes: who did what to which item |

Changing (only with `--allow-changes`, and only within the account's permissions):

| Tool | What it does |
| --- | --- |
| `add_hosts_to_group` | Adds hosts, by full host name, to a host group |
| `remove_hosts_from_group` | Takes hosts out of a group, or excludes those that come from its rule or an included group |
| `remove_host_everywhere` | Lists what a host is in; with `confirm` set, takes it out of every group and deletes it |
| `set_task_enabled` | Enables or disables one task of a mesh |
| `update_test` | Changes a test's options and how often it repeats, after checking them against pScheduler's schema |

Every tool is marked read-only or not, and `remove_host_everywhere` as destructive, so assistants that ask
before acting know which calls to ask about.

## What comes next

1. **Remote server for everyone with a login.** MCP's Streamable HTTP transport at `/mcp` on the pwa-next
   host. Two ways to check who is calling, in this order:
   - *Personal access tokens:* each person makes a token on their own People page (shown once, stored
     hashed, with an expiry and a "can change things" switch, revocable). The assistant sends it as a bearer
     token; Apache lets `/mcp` through without the CILogon sign-in and pwa-next checks the token. Works with
     every MCP client today.
   - *MCP's OAuth sign-in through CILogon:* the assistant opens a CILogon login in the browser, as the web
     pages do. This needs CILogon to accept the dynamic client registration MCP clients use; to be confirmed
     with the CILogon team.
2. **More change tools**, in the order the WLCG clean-up needs them: remove many gone hosts at once, replace a
   test or group everywhere, create host groups with rules, create tasks, manage teams and access.
3. **Resources** (the published files, the History feed) and **prompts** for routine jobs such as the weekly
   check of gone-quiet hosts.
