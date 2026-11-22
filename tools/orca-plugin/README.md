# amemo for Orca IDE

Open **amemo** in Orca's right sidebar. Search the local memory, open a result
to read the matched revision and its sources, or check the service and review
queue in **Status**. The worker sends GET requests to
`http://127.0.0.1:8767/api/v1/` and expects the local, no-auth configuration.
Start the amemo service on that port on the machine running Orca Desktop.

The **Agent** tab prepares the Claude plugin's Remember, Verify and Ingest
workflows for an active Orca agent session. Paste the prompt into an agent that
has amemo MCP configured. From the agent's host, amemo's existing
`amemo install codex --project <dir>` and `amemo install claude-code` installers
configure MCP, agent hooks and core skills. Orca's native plugin API cannot
register those agent integrations itself.

Install from the desktop host with `orca plugin install <path-to-this-folder>`.
Orca displays the trusted-worker approval for the local HTTP calls. From WSL,
invoke the Windows Orca CLI with a Windows or UNC path to the folder.

Validate with `orca plugin validate <folder>` and `orca plugin check <folder>`.
No JavaScript dependencies or build step are required.
