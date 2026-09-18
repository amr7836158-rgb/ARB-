# -*- coding: utf-8 -*-
"""
bot.py
======
بوت تلجرام لترجمة ملفات الدراسة (PDF / PPTX) إلى العربية كـ Study Annotation.

الخصوصية: الملف يدخل الذاكرة (RAM) فقط، يُعالج، يُرسل للمستخدم، ثم تُحذف
كل البيانات من الذاكرة. لا حفظ على القرص، لا قاعدة بيانات، لا سجل.

التشغيل:
    BOT_TOKEN=... python bot.py
اختياري:
    MAX_FILE_SIZE_MB=20
    TRANSLATION_API_URL=...  TRANSLATION_API_KEY=...
    HANDWRITING_FONT=...  HANDWRITING_SIZE=15  HANDWRITING_COLOR="35,70,150"
"""

from __future__ import annotations

import asyncio
import gc
import io
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

import translator

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "20"))

# رسائل التقدم (كما طلبتها حرفيًا)
STAGE_MESSAGES = {
    translator.STAGE_EXTRACT: "جاري استخراج النص...",
    translator.STAGE_TRANSLATE: "جاري الترجمة...",
    translator.STAGE_FORMAT: "جاري تنسيق الملاحظات...",
    translator.STAGE_CHECK: "جاري فحص أماكن الترجمة...",
}

MSG_RECEIVED = "تم استلام الملف، جاري التحليل..."
MSG_DONE = "تم الانتهاء."
MSG_UNSUPPORTED = "أرسل PDF أو PPTX فقط."
MSG_TOO_BIG = "حجم الملف أكبر من الحد المسموح."
MSG_CORRUPT = "تعذر قراءة الملف."
MSG_FAILED = "حدث خطأ أثناء الترجمة، حاول مرة أخرى."

router = Router()
logger = logging.getLogger("study-bot")


def _file_kind(document: Message.Document) -> str | None:
    name = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()
    if name.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    if name.endswith(".pptx") or "presentationml.presentation" in mime:
        return "pptx"
    return None


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "أهلًا! 👋\n"
        "أرسل ملف PDF أو PPTX وسأضيف ترجمة عربية كملاحظات مذاكرة "
        "بجانب النص الأصلي في نفس الصفحة/الشريحة.\n\n"
        "🔒 الملف لا يُحفظ في أي مكان: تتم المعالجة في الذاكرة فقط "
        "ويُحذف كل شيء فور إرسال الملف المترجم."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "طريقة الاستخدام:\n"
        "1) أرسل ملف PDF أو PPTX (الحد الأقصى "
        f"{MAX_FILE_SIZE_MB} ميجابايت).\n"
        "2) استنى رسائل التقدم لحد ما الملف المترجم يوصلك.\n\n"
        "ملاحظات:\n"
        "• لو الملف PDF عبارة عن صور ممسوحة ومفيش نص، هيتم محاولة OCR تلقائيًا.\n"
        "• مفيش أي حفظ أو أرشفة للملفات أو للترجمات."
    )


@router.message(F.document)
async def handle_document(message: Message, bot: Bot):
    kind = _file_kind(message.document)
    if kind is None:
        await message.answer(MSG_UNSUPPORTED)
        return

    file_size = message.document.file_size or 0
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(MSG_TOO_BIG)
        return

    status = await message.answer(MSG_RECEIVED)
    loop = asyncio.get_running_loop()

    input_buffer = io.BytesIO()   # المستخدم → RAM
    output_buffer = None          # الناتج → RAM
    data = b""
    try:
        tg_file = await bot.get_file(message.document.file_id)
        await bot.download(tg_file, destination=input_buffer)
        input_buffer.seek(0)
        data = input_buffer.getvalue()

        def push(stage: str) -> None:
            """تحديث رسالة التقدم من thread المعالجة."""
            text = STAGE_MESSAGES.get(stage)
            if text:
                asyncio.run_coroutine_threadsafe(status.edit_text(text), loop)

        if kind == "pdf":
            output_buffer = await asyncio.to_thread(translator.translate_pdf, data, push)
        else:
            output_buffer = await asyncio.to_thread(translator.translate_pptx, data, push)

        original_name = message.document.file_name or f"file.{kind}"
        base = original_name.rsplit(".", 1)[0] or "file"
        out_name = f"{base}-ar.{kind}"

        await status.edit_text(MSG_DONE)
        await message.answer_document(
            BufferedInputFile(output_buffer.getvalue(), filename=out_name),
            caption="📖 الترجمة العربية كملاحظات مذاكرة — الملف الأصلي كما هو.",
        )
    except ValueError:
        logger.warning("ملف غير قابل للقراءة", exc_info=True)
        await status.edit_text(MSG_CORRUPT)
    except Exception:
        logger.exception("فشل في معالجة الملف")
        try:
            await status.edit_text(MSG_FAILED)
        except Exception:
            pass
    finally:
        # حذف كل بيانات المهمة من الذاكرة — لا أثر بعد الإرسال
        input_buffer.close()
        if output_buffer is not None:
            output_buffer.close()
        data = b""
        input_buffer = None  # type: ignore[assignment]
        output_buffer = None
        gc.collect()


@router.message()
async def fallback(message: Message):
    await message.answer(MSG_UNSUPPORTED)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not BOT_TOKEN:
        raise SystemExit("خطأ: يجب تعيين BOT_TOKEN في متغيرات البيئة.")

    provider = translator.active_provider()
    provider_names = {
        "gemini": "Google Gemini — مفتاح API مجاني (جودة أعلى)",
        "deepl": "DeepL API — مفتاح مجاني",
        "openai": "OpenAI API (ChatGPT) — مفتاح مدفوع بالاستخدام",
        "custom": "خدمة ترجمة مخصصة",
        "free": "ترجمة مجانية عامة بدون مفتاح — لجودة أعلى ضع مفتاح Gemini مجاني من Google AI Studio",
    }
    logger.info("محرك الترجمة: %s", provider_names.get(provider, provider))

    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logger.info("البوت يعمل...")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
