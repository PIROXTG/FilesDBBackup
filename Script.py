class script(object):

    START_TXT = """<b>✨ Welcome, {user}!</b>

<blockquote><b>🤖 File Search Bot</b>
Send me a movie or series name and I'll find the files for you.</blockquote>

<b>🔍 Just type a name to search!</b>"""

    HELP_TXT = """<b>📚 Help</b>

<blockquote><b>🎬 Movies:</b> <code>Movie Year</code> → <code>RRR 2022</code>
<b>📺 Series:</b> <code>Name S01E05</code> → <code>Loki S01E05</code></blockquote>

<b>💡 Tip:</b> add quality or language → <code>1080p</code> <code>Tamil</code> <code>Hindi</code>

<b>Commands</b>
/start - start the bot
/help - this message
/search <code>name</code> - search files"""

    ADMIN_HELP_TXT = """

<b>🛠 Admin commands</b>
/stats - database statistics
/index <code>link</code> - index a channel (or send a link / forward a post in PM)
/setskip <code>number</code> - set the indexing start position
/status - indexing status
/forward <code>[skip] channel ids</code> - forward files to channels (no arguments resumes)
/forwardstop - stop forwarding
/forwardstatus - forwarding progress
/update - git pull and restart
/restart - restart the bot"""

    GROUP_START_TXT = "<b>👋 Hi! Send a movie or series name and I'll search for it.</b>"
