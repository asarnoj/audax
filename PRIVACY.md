# Privacy policy

Audax is a personal agent that one person runs for themselves, on their own hardware. It is not a service offered to anyone else, and the OAuth client used for Gmail is used only by its owner.

**What it accesses.** With the owner's consent, Audax reads the owner's Gmail with the read-only scope `gmail.readonly`. It cannot send, delete or change mail, and the engine refuses a token with any wider scope.

**How the data is used.** Mail is read on demand to give the owner's own agent context: a short overview for its nightly review, and searches when the owner asks. The content is processed by Claude (Anthropic) under the owner's own subscription, in the same way as everything else the owner asks the agent.

**What is stored.** The engine does not store mail. The agent may write short conclusions (for example "replied on 14 October, interview on 22 October") into the owner's private notes, which stay on the owner's own machines and backups.

**Sharing.** Nothing is sold, shared with third parties or used for advertising. Google user data is not used to train models.

**Revoking access.** The owner can revoke access at any time at https://myaccount.google.com/permissions.

Contact: through the GitHub repository, https://github.com/asarnoj/audax.
