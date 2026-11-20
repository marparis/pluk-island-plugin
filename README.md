# pluk_notify

The Hermes plugin of the PLUK Island Android app: it tells the phone when an agent needs you, answered or failed
(through your own Firebase project), gives the voice companion its single-use token, and keeps itself up to date from
the app.

It is installed from the app (Settings › Notifications), or from Hermes: `hermes plugins install` with this repository.
Nothing in this repository is personal: no address, no key, no account. What the plugin needs (your Firebase service
account, your Google key for the voice) stays in files on your own server.

Parts of it follow hermes-conduit-notifier (MIT): see `LICENSE-CONDUIT`.
