# Privacy Policy — vibe-cognition
**Acrylic Code Inc.**
Effective date: November 18, 2026

vibe-cognition is a plugin for Claude Code and OpenAI Codex CLI that keeps a knowledge graph of your project on your own computer. **Acrylic Code does not collect, receive or store any of your data.** The plugin has no account, no telemetry, no analytics and no Acrylic Code server. Everything it records stays in your project folder and on your machine. It leaves your computer only through the AI assistant you use it with, as described under "Your AI assistant", and wherever you send it yourself, for example when you commit it to your own version control.

## Who this policy covers

This policy covers the vibe-cognition plugin, published by Acrylic Code Inc. and available at [github.com/Haagndaazer/vibe-cognition](https://github.com/Haagndaazer/vibe-cognition). It describes what the plugin does with data on the computer where it is installed.

It does not cover the AI assistant you run the plugin in, the services listed under "Network connections the plugin makes", or the place you host your code. Each of those has its own privacy policy. If you open an issue or pull request on GitHub, what you post there is public and is handled under GitHub's own privacy policy.

## What Acrylic Code collects

Nothing. The plugin sends no information about you, your computer, your project or your knowledge graph to Acrylic Code, and Acrylic Code operates no service that the plugin connects to. We cannot see, read, export or delete your data, because we never have it.

## What the plugin stores on your computer

The plugin keeps its data in two places.

**In your project, in a folder named .cognition:**

- **The knowledge graph journal.** The memories your AI assistant records while you work: decisions, discoveries, failures, constraints, patterns, tasks, procedures and summaries of completed work, and the links between them. These are plain text files. Each person writes to a file named after their email address, and projects started on older versions also have one shared file. Every entry carries the name and email address of the person who recorded it and the time it was written.
- **Stored documents.** When you ask for a document to be kept as project memory, the plugin stores the text extracted from it and, if you choose, a copy of the file itself. The journal entry for the document also records the file's name, size, the opening part of its text, and the full location the file had on your computer. That location usually includes your operating system account name.
- **The people list.** For each person on the project: name, email address, role, seniority and the email address of the person they report to. This list is created when a person confirms their identity or when a teammate registers them.
- **Environment facts.** Optional notes about a person's machine setup, such as folder locations or installed tools, stored under that person's email address and the name of the machine.
- **A local identity file.** It records the name and email address you confirmed for that copy of the project, the name of your computer, and scrambled fingerprints of your operating system account name and the project folder location. This file stays on your machine and is not committed.

**In the plugin's own data folder on your computer, outside your project:**

- **A search index** built from the text of your memories and documents, used to find them by meaning. It can be deleted at any time and is rebuilt from the journal.
- **The plugin's software environment** and a few small marker files, such as the time of the last update check.

The embedding model that powers search is kept in the standard Hugging Face cache folder on your computer. The plugin also writes short startup timing notes to your system's temporary folder; they contain no project content and are cleared after seven days.

**Other changes the plugin makes.** In a git project it adds a few lines to the project's ignore and attributes files so the graph files merge cleanly and machine-local files stay uncommitted. In a Subversion project it schedules new graph files to be added and sets an ignore property. On Codex CLI it registers itself in your personal Codex configuration and installs an agent role file there. The [README](https://github.com/Haagndaazer/vibe-cognition#what-it-runs-downloads-and-sends) describes each of these and how to opt out.

## Information the plugin reads from your computer

- **Your name and email address from your git configuration**, and any usernames saved by Subversion on your machine. They are offered as suggestions when you confirm your identity. Nothing is attributed to you until you confirm it.
- **Your project's commit history**, including commit messages and author names, only when you ask the plugin to build memories from past commits, or to work out who recorded memories made before the plugin tracked identities.
- **The name of your computer**, used for the identity file and for environment facts.
- **Your operating system account name**, used to make the scrambled fingerprint in the identity file and, when nothing better is found, offered as a suggested name.

All of this stays on your computer, apart from what ends up in the shared files described in the next section.

## What travels with your project

The .cognition folder is designed to be committed to your own version control (git or Subversion), so that your team shares one project memory. When you commit and push it, the following goes wherever your repository goes, and can be read by anyone who can read the repository:

- every memory in the journal, with the recording person's name and email address
- the people list, with names, email addresses, roles and reporting lines
- environment facts, including machine names
- for every stored document: its extracted text, its file name, and the full location the file had on your computer, which usually includes your operating system account name
- the copied file of a stored document, unless you marked that document as local-only. Local-only keeps only the copied file out of version control; the items in the previous point are still shared

Three points deserve attention:

- **A public repository makes all of this public.** Think about that before committing the folder to an open-source project.
- **Personal constraints are hidden, not secret.** A working preference recorded as personal is hidden from teammates inside the plugin, but it is still a line in a committed file that anyone with access to the repository can open.
- **The plugin never commits or pushes by itself.** Your AI assistant can run version control commands if you allow it to, so you decide whether, when and where these files are shared.

The local identity file, the search index and other machine-local files are kept out of version control by ignore rules the plugin writes. On Subversion those rules do not cover every case; the [README](https://github.com/Haagndaazer/vibe-cognition#subversion-teams) explains what to check before committing.

## Network connections the plugin makes

The plugin makes the following connections. None of them carries your project content or your graph. As with any internet request, each service sees your IP address and ordinary technical details of the request.

- **Installing the software.** On first install and after updates, Python packages are downloaded from the Python Package Index (pypi.org) and from PyTorch's package index (download.pytorch.org). If your computer does not already have a suitable version of Python, uv, the installer the plugin uses, downloads one as well.
- **Downloading the embedding model.** On first start, the model is downloaded from Hugging Face (huggingface.co). The model comes with its own loading code, which is downloaded with it and runs on your computer. On later starts the Hugging Face software may contact huggingface.co to check whether the cached copy is current. You can fix the model to a version you have reviewed, and turn the later checks off once the model is downloaded.
- **A daily update check.** Once every 24 hours at most, the plugin reads two small public files from GitHub (raw.githubusercontent.com) to learn whether a newer version exists. The request carries no information about you or your project. On Claude Code it runs only for installs from the coltondyck marketplace; on Codex CLI it runs for every install. It can be turned off.

The [README](https://github.com/Haagndaazer/vibe-cognition#what-it-runs-downloads-and-sends) lists every connection and the setting that turns each one off.

**Optional Ollama search.** If you choose to switch the search backend to Ollama, the text of your memories, your stored documents and your search queries is sent to the Ollama server you configure. By default that server is your own computer. If you point it at a server on another machine, that text is sent there.

## Your AI assistant

The plugin exists so that your AI assistant (Claude Code or Codex CLI) can read and write the knowledge graph. At the start of each session, and again when the assistant condenses a long conversation, the plugin gives the assistant a short summary of the graph, such as open tasks, active constraints and recent decisions, and, for a registered person, their name, role and who they report to. Whenever the assistant searches or reads a memory, that content is added to the conversation as well. Everything in the conversation is handled by the assistant's provider under that provider's terms and privacy policy, in the same way as the source files the assistant reads. The plugin makes no network connection to those providers itself.

## The dashboard

The dashboard is an optional web page for browsing the graph. It runs only when you ask for it, is served from your own computer at a local-only address that other machines cannot reach, and is protected by an access token that is generated each time it starts. The address containing that token is given to your AI assistant so it can show it to you. It opens in your default browser and loads no outside resources. From the dashboard you can view the graph and delete a memory; it cannot make any other change.

## Reading another project's graph

You can ask the plugin to attach the knowledge graph of another project folder on your computer so the assistant can search it. That access is read-only: the plugin never writes to the other project. Nothing is sent anywhere as a result.

## Children

The plugin is a tool for software developers. It is not directed at children, and Acrylic Code does not collect personal information from anyone, including children.

## How long data is kept

Your data is kept until you delete it. Acrylic Code holds no copy and applies no retention period.

## How to delete your data

Because all of the data is on your computer and in your own repository, you delete it yourself:

- **One memory.** Ask your assistant to delete it, or delete it from the dashboard. It leaves the graph and the search index. The original line stays in the journal file together with a deletion marker, so it remains readable to anyone who opens that file.
- **Your environment facts.** Ask your assistant to clear everything stored about your machine setup. The cleared lines likewise remain in the file's history.
- **A person on the people list.** Ask your assistant to remove them from the roster. Their profile is cleared, but its earlier lines (name, email address, role and reporting line) remain in the profile file. The memories they recorded stay in the graph, and their environment facts stay until they clear them themselves.
- **A stored document.** Ask your assistant to delete the document's memory. This also removes the stored text and the copied file from the folder. The document's original journal line, including the file's original location and the opening part of its text, stays in the journal file.
- **Everything for a project.** Delete the .cognition folder.
- **The search index and the plugin's software.** Delete the plugin's data folder.
- **The embedding model.** Delete it from the Hugging Face cache folder.

The [README](https://github.com/Haagndaazer/vibe-cognition#uninstall--cleanup) gives the exact folder locations.

**Version control history is separate.** If the .cognition folder was committed, earlier versions of its files remain in your repository's history and in every copy of the repository until you remove them with your version control system's own tools. If you need someone else's copy removed, ask the person or organisation that controls that repository. Acrylic Code cannot do this for you.

## Your rights

Privacy laws may give you rights to access, correct or delete personal information that an organisation holds about you. Acrylic Code holds no personal information about users of the plugin, so there is nothing for us to provide, correct or delete. If your name or email address appears in a project's knowledge graph, the person or organisation that controls that project's repository is the one to ask.

If you contact us by email, we use your email address and message only to reply to you, and keep them only as long as needed for that.

## Changes to this policy

If the plugin's handling of data changes, we will update this policy and its effective date. The current version is always published on this page, and the source text is kept in the plugin's public repository, where its history can be reviewed.

## Contact

- Acrylic Code Inc.
- Alberta, Canada
- [colton.dyck@acryliccode.com](mailto:colton.dyck@acryliccode.com)
