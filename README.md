# mailroom

Sort and clean up a Gmail account from the command line.

- **Sorting:** Claude (via `claude -p` on your Claude Code login, no API key) picks a category per sender:
  Purchases, Promotions, Newsletters, Finance, School, Travel, Accounts, Notifications, Social, Personal, Other.
  Senders that send different kinds of mail (a shop: orders *and* ads) are sorted mail by mail.
  Each category becomes a Gmail label; mail stays in the inbox.
- **New mail:** Gmail filters label new mail from known senders on arrival; `mailroom sync && mailroom classify && mailroom label` catches the rest.
- **Cleanup:** see what takes the space (`stats`), then move mail to the trash by category, sender, age or size.
  Dry run by default; starred and important mail is kept unless you say otherwise.

Only senders, subjects and snippets are sent to Claude, never mail bodies. The cache lives in
`~/.local/share/mailroom/`, the login in `~/.config/mailroom/`.

**Safety:** mailroom asks only for `gmail.modify` and `gmail.settings.basic`, so it *cannot* delete mail
permanently. Trash keeps mail for 30 days.

## Google Cloud setup (once, ~10 minutes)

1. Open <https://console.cloud.google.com/>, create a project (e.g. `mailroom`).
2. **APIs & Services → Library →** search *Gmail API* → **Enable**.
3. **Google Auth Platform → Branding / Audience:** app name `mailroom`, your email as support and
   developer contact; user type **External**; under **Audience → Test users** add your Gmail address.
4. **Clients → Create client → Desktop app** → download the JSON and save it as
   `~/.config/mailroom/client_secret.json`.
5. `mailroom login` → a browser opens; Google warns that the app is unverified (it's yours) → *Continue*.

While the app is in *Testing*, Google expires the login after 7 days; just run `mailroom login` again.

## Usage

```bash
uv tool install .            # or: uv run mailroom ...
mailroom login
mailroom sync                # first run fetches metadata of all mail (resumable)
mailroom stats               # where the space goes
mailroom classify            # Claude sorts senders (stops at 80 % of the 5-hour window)
mailroom senders -c Promotions
mailroom set orders@shop.cz Purchases   # fix a sender
mailroom label --dry-run && mailroom label
mailroom filters             # label new mail on arrival
mailroom trash -c Promotions --older-than 365          # dry run
mailroom trash -c Promotions --older-than 365 --yes    # really
```

## Development

```bash
uv run pytest
```
