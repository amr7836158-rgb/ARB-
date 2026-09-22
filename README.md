
# نظام إدارة وجدولة تفاعلات بوتات Telegram

نظام مركزي يدير مجموعة بوتات حقيقية عبر Bot Tokens، يرصد منشورات القنوات،
وينشئ Queue، وينفّذ التفاعل عبر Bot API الرسمي فقط.

## المحتويات
- main.py            النظام كاملًا في ملف واحد
- requirements.txt   المكتبات
- .env               الإعدادات (يحتوي التوكن المدمج)
- render.yaml        تهيئة النشر على Render

## التشغيل المحلي
1. pip install -r requirements.txt
2. python main.py

## أوامر البوت الرئيسي
/start /addbot /bots /botinfo /enablebot /disablebot
/channels /scan /channelinfo /bindbot /unbindbot
/setreaction /setfallback /setdelay /status /queue /settings /logs /pause /resume

## خطوات الربط
1. أنشئ البوتات من @BotFather.
2. /addbot وأرسل Token كل بوت.
3. أضِف البوت الرئيسي والبوتات إلى القناة كـ Administrator من تليجرام.
4. ستظهر القناة تلقائيًا في /channels (أو استخدم /scan).
5. /setreaction ثم /setdelay.
