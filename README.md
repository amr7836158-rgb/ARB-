# بوت ترجمة المحاضرات 🎓

بوت Telegram يترجم ملفات PDF و PPTX إلى العربية كملاحظات مذاكرة مكتوبة بخط اليد
بجانب النص الأصلي — في نفس الصفحة/الشريحة، بدون تغيير التصميم.

🔒 الملف يُعالج في الذاكرة (RAM) فقط ويُحذف كل شيء فور إرسال الناتج — لا حفظ ولا أرشفة.

## التشغيل (3 أوامر)

```bash
python3 -m venv venv && source venv/bin/activate   # ويندوز: venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

ملف `.env` جاهز بالفعل بالتوكن والمفتاح — لا تحتاج لتعديل أي شيء.

## الاستخدام

1. افتح البوت في Telegram واضغط Start
2. أرسل ملف PDF أو PPTX (حتى 20 ميجابايت) كـ File
3. استنى رسائل التقدم ثم يوصلك الملف المترجم باسم `اسم-الملف-ar.pdf` أو `-ar.pptx`

## الإعدادات (ملف .env)

| المتغير | الوصف | الافتراضي |
|---|---|---|
| `BOT_TOKEN` | توكن البوت من @BotFather | مطلوب |
| `TRANSLATION_API_KEY` | مفتاح Gemini (يُكتشف تلقائيًا) أو DeepL (`:fx`) أو OpenAI (`sk-`) | بدون مفتاح = ترجمة مجانية عامة |
| `TRANSLATION_MODEL` | موديل Gemini | `gemini-3.6-flash` |
| `MAX_FILE_SIZE_MB` | حد حجم الملف | 20 |
| `HANDWRITING_FONT` | مسار خط يد عربي `.ttf` (اختياري — يحسّن الشكل) | خط النظام |
| `HANDWRITING_SIZE` | حجم خط الترجمة | 15 |

## تحذير أمني ⚠️

التوكن والمفتاح الموجودان في `.env` تمت مشاركتهما في محادثة، أي شخص يراهما
يمكنه استخدامهما. **يُنصح بتجديدهما**:
- التوكن: من @BotFather → `/revoke`
- مفتاح Gemini: من [Google AI Studio](https://aistudio.google.com/app/api-keys) → احذف المفتاح وأنشئ جديدًا
