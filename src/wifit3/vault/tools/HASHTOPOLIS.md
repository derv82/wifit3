# Hashtopolis

The Hashtopolis vault tool uploads a capture's WPA hashes to a Hashtopolis server, runs one cracking
task on a wordlist that is already on the server, and polls until the server cracks the key or the
wordlist runs out. A recovered key is saved to the Vault, the same as with CPU Crack and Hashcat.

## Server versions

A JWT access key uses API v2 (`/api/v2`). Any other key uses the legacy User API (`/api/user.php`).
- API v2 tokens exist since 1.0.0-rainbow6 (2026-04). On 1.1.0 the User API is deprecated.
- 0.14.x keeps API v2 off by default, so those servers still need a User API key.

Tested live:

| Server | Agent | Cracked | Not found | Stop |
|---|---|---|---|---|
| 1.0.1 | python agent 0.7.6, hashcat 7.1.2 on CPU | yes | yes | yes |
| 0.14.8 | same | yes | yes | yes |

Every User API call the tool makes exists since 0.12.0, going by the server's git history; servers older than 0.14.8 are untested.

## Setup

1. **Access key.** In the new UI: Account → API Keys → New API Key. Paste the token (shown once)
   as the access key. It needs to read files, access groups and crackers, and to create hashlists
   and tasks. A legacy key from the old UI (Users → API Management, on the backend port) still works.
2. **Key lifetime.** Set it when creating the key. A legacy key expires after 30 days unless its
   edit page changes that. An expired key fails the job with the server's message.
3. **Configure in wifit3.** Open the Hashtopolis modal from the Vault or from Preferences (≡ next to
   the auto-crack dropdown). Enter:
   - the server URL, e.g. `http://192.168.1.10:8080`
   - the access key
   
   Then press Test / Refresh and choose a wordlist and an access group.

## Wordlists

wifit3 only lists the server's wordlist files (`fileType` 0); it never uploads one. To add a
wordlist, upload it in the Hashtopolis UI, then press Test / Refresh.

A task uses one wordlist. Hashtopolis can't measure the keyspace of two dictionary files in one
hashcat command, so such a task never starts.

## When a task never starts

A job that no agent picks up shows "Waiting for an agent" until you stop it. Hashtopolis reports no
error for this. The usual causes are below; fix them in the Hashtopolis UI.

| Cause | Fix |
|---|---|
| The wordlist is secret and the agent is not trusted | Trust the agent, or make the file non-secret |
| The agent's CPU-only setting doesn't match the task (wifit3's tasks are not CPU-only) | Turn off CPU-only on the agent |
| The agent is inactive (e.g. after a failed benchmark) | Set the agent active |
| No agent is in the task's access group | Add the agent to that group |

## FT-PSK

Only `WPA*01` (PMKID) and `WPA*02` (EAPOL) lines are uploaded. FT-PSK lines (`WPA*03`, `WPA*04`) are not:
- hashcat 7.1.2 rejects them.
- On a Hashtopolis server, that failure deactivates the agent for every task, not just this one.

A capture with only FT-PSK lines doesn't offer Hashtopolis, and auto-crack skips it there.

## Behaviour

- **Stop** archives the task. The agent drops it within one status interval, and the task and any
  cracks stay on the server.
- **Restart.** A job that is running when wifit3 closes resumes polling on the next start. If the
  server URL changed in between, the job ends with an error rather than polling the new server.
- **Server down.** An unreachable server keeps the job running and retries every 5 seconds.
  - Errors the server reports (bad key, permission denied) end the job with the server's message.
- **Access key storage.** The key is stored only in `config.toml`. Jobs record the server URL,
  wordlist, access group and the server's task and hashlist ids, never the key.
