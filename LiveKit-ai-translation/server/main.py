import asyncio
import logging
import json
import time
import re
import websockets
from typing import Set, Any

from enum import Enum
from dataclasses import dataclass, asdict

from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    JobRequest,
    WorkerOptions,
    cli,
    stt,
    llm,
    utils,
)
from livekit.plugins import openai, silero, speechmatics
from livekit.plugins.speechmatics.types import TranscriptionConfig
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger("transcriber")

# WebSocket server for display clients
connected_displays: Set[Any] = set()

async def register_display(websocket):
    """Register a new display client"""
    global connected_displays
    
    connected_displays.add(websocket)
    logger.info(f"Display client connected. Total displays: {len(connected_displays)}")
    try:
        await websocket.wait_closed()
    finally:
        connected_displays.discard(websocket)
        logger.info(f"Display client disconnected. Total displays: {len(connected_displays)}")

async def broadcast_to_displays(message_type: str, language: str, text: str):
    """Send transcription/translation to external WebSocket server for display"""
    message = {
        "type": message_type,
        "language": language,
        "text": text,
        "timestamp": time.time(),
        "source": "livekit"
    }
    
    message_json = json.dumps(message)
    logger.info(f"🎤 REAL: Sending to display server: {message_type} ({language}): {text[:50]}...")
    
    # Send to external WebSocket server via HTTP POST
    # (the external server will handle broadcasting to connected displays)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post('http://localhost:8766/broadcast', 
                                   json=message,
                                   timeout=aiohttp.ClientTimeout(total=1)) as response:
                if response.status == 200:
                    logger.debug("Successfully sent to external display server")
                else:
                    logger.warning(f"Display server responded with status {response.status}")
    except Exception as e:
        logger.debug(f"Could not send to display server (this is normal if no display server is running): {e}")

async def start_websocket_server():
    """Start WebSocket server for display clients"""
    logger.info("Starting WebSocket server for display clients on port 8765...")
    server = await websockets.serve(register_display, "localhost", 8765)
    logger.info("WebSocket server started on ws://localhost:8765")
    return server


@dataclass
class Language:
    code: str
    name: str
    flag: str


languages = {
    "ar": Language(code="ar", name="Arabic", flag="🇸🇦"),
    "en": Language(code="en", name="English", flag="🇺🇸"),
    "es": Language(code="es", name="Spanish", flag="🇪🇸"),
    "fr": Language(code="fr", name="French", flag="🇫🇷"),
    "de": Language(code="de", name="German", flag="🇩🇪"),
    "ja": Language(code="ja", name="Japanese", flag="🇯🇵"),
    "nl": Language(code="nl", name="Dutch", flag="🇳🇱"),  # Added Dutch
}

LanguageCode = Enum(
    "LanguageCode",  # Name of the Enum
    {lang.name: code for code, lang in languages.items()},  # Enum entries: name -> code mapping
)


class Translator:
    # 🔧 TOGGLE: Set to False to disable rolling context (fresh context each translation)
    use_context = False  # Change to True to enable rolling 9-sentence context
    
    def __init__(self, room: rtc.Room, lang: Enum):
        self.room = room
        self.lang = lang
        # Fixed: Use append_message instead of append
        self.context = llm.ChatContext()
        self.message_count = 0  # Track number of user messages
        self.system_prompt = (
            f"You are a simultaneous interpreter for a live islamic religious friday sermon. "
            f"Translate only the **most recent user sentence** into {lang.value}. The last 9 translated sentances are shown as context so you know how to translate the most recent one"
            f"Return **exactly that one sentence** in {lang.value}—no summaries, no commentary, be concise and to the point and use words that common in speech "
            f"no repetition of earlier lines. make sure to translate in {lang.value} only. "
        )
        self.context.add_message(role="system", content=self.system_prompt)
        self.llm = openai.LLM()
        
        # Log the context mode being used
        context_mode = "ROLLING CONTEXT (9-message memory)" if self.use_context else "FRESH CONTEXT (no memory)"
        logger.info(f"🧠 Translator initialized for {lang.value} with {context_mode} mode")

    async def translate(self, message: str, track: rtc.Track):
        if self.use_context:
            # ROLLING CONTEXT MODE: Use accumulated context with 9-message limit
            logger.debug(f"🔄 Using ROLLING CONTEXT mode (message #{self.message_count + 1})")
            self.context.add_message(content=message, role="user")
            self.message_count += 1
            
            # Keep only latest 9 user messages - recreate context when needed
            if self.message_count > 9:
                logger.info(f"🔄 Resetting context after 9 messages")
                self.context = llm.ChatContext()
                self.context.add_message(role="system", content=self.system_prompt)
                self.message_count = 0
                
            stream = self.llm.chat(chat_ctx=self.context)
        else:
            # FRESH CONTEXT MODE: Create new context for each translation (no memory)
            logger.debug(f"🆕 Using FRESH CONTEXT mode (no memory)")
            fresh_context = llm.ChatContext()
            fresh_context.add_message(role="system", content=self.system_prompt)
            fresh_context.add_message(content=message, role="user")
            stream = self.llm.chat(chat_ctx=fresh_context)
        translated_message = ""
        async for chunk in stream:
            if chunk.delta is None:
                continue
            content = chunk.delta.content
            if content is None:
                break
            translated_message += content

        segment = rtc.TranscriptionSegment(
            id=utils.misc.shortuuid("SG_"),
            text=translated_message,
            start_time=0,
            end_time=0,
            language=self.lang.value,
            final=True,
        )
        transcription = rtc.Transcription(
            self.room.local_participant.identity, track.sid if track else "", [segment]
        )
        await self.room.local_participant.publish_transcription(transcription)

        # Also broadcast translation to WebSocket displays
        asyncio.create_task(
            broadcast_to_displays("translation", self.lang.value, translated_message)
        )

        print(
            f"message: {message}, translated to {self.lang.value}: {translated_message}"
        )


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(job: JobContext):
    # Configure source language - ARABIC as default
    # This will be the language that users are actually speaking (host/speaker language)
    source_language = "ar"  # Arabic is the default source language - host speaks Arabic
    
    # Configure Speechmatics STT for Arabic speech recognition
    # Speechmatics supports Arabic language recognition with enhanced settings
    stt_provider = speechmatics.STT(
        transcription_config=TranscriptionConfig(
            language="ar",
            operating_point="enhanced",
            enable_partials=True,
            max_delay=3.5,
            punctuation_overrides={"sensitivity": 0.5},
            diarization="speaker"
        )
    )  # Configure for Arabic using Speechmatics with partials, low delay, and speaker diarization
    
    tasks = []
    translators = {}
    
    # Create hardcoded Dutch translator (revert to working version)
    dutch_enum = getattr(LanguageCode, 'Dutch')
    translators["nl"] = Translator(job.room, dutch_enum)
    
    # Sentence accumulation for proper sentence-by-sentence translation
    accumulated_text = ""  # Accumulates text until we get a complete sentence
    last_final_transcript = ""  # Keep track of the last final transcript to avoid duplicates
    translation_delay = 10.0  # Reduced to 2 seconds for faster incomplete sentence translation
    pending_translation_task = None
    
    logger.info(f"🚀 Starting entrypoint for room: {job.room.name if job.room else 'unknown'}")
    logger.info(f"📝 Initialized hardcoded Dutch translator (nl)")
    logger.info(f"🔍 Translators dict ID: {id(translators)}")
    logger.info(f"🗣️ STT configured for {languages[source_language].name} speech recognition using Speechmatics (source language: {source_language})")
    logger.info(f"🇸🇦 ARABIC is set as the default host/speaker language")

    def _extract_complete_sentences(text: str):
        """Extract complete sentences from text and return them along with remaining incomplete text"""
        if not text.strip():
            return [], ""
        
        # Use regex to find sentence endings - be more flexible with sentence detection
        # Added Arabic punctuation marks (Speechmatics STT doesn't add commas to Arabic)
        sentence_pattern = r'([.!?؟،]+)'
        parts = re.split(sentence_pattern, text)
        
        complete_sentences = []
        remaining_text = ""
        
        i = 0
        while i < len(parts):
            if i + 1 < len(parts) and re.match(r'[.!?؟،]+', parts[i+1]):
                # This is a complete sentence
                sentence = (parts[i] + parts[i+1]).strip()
                if sentence and not sentence.isspace():
                    complete_sentences.append(sentence)
                i += 2
            else:
                # This might be incomplete text at the end
                remaining_text = parts[i].strip()
                break
        
        return complete_sentences, remaining_text

    async def _translate_sentences(sentences: list[str]):
        """Translate complete sentences to all target languages"""
        if not sentences or not translators:
            return
            
        for sentence in sentences:
            if sentence.strip():
                logger.info(f"🎯 TRANSLATING COMPLETE ARABIC SENTENCE: '{sentence}'")
                logger.info(f"📊 Processing sentence for {len(translators)} translators")
                
                # Send to all translators concurrently for better performance
                translation_tasks = []
                for lang, translator in translators.items():
                    logger.info(f"📤 Sending complete Arabic sentence '{sentence}' to {lang} translator")
                    translation_tasks.append(translator.translate(sentence, None))
                
                # Execute all translations concurrently
                if translation_tasks:
                    await asyncio.gather(*translation_tasks, return_exceptions=True)

    async def _delayed_translation(text: str, delay: float):
        """Wait for delay, then translate incomplete text if no new updates came in"""
        nonlocal pending_translation_task
        
        try:
            await asyncio.sleep(delay)
            # Check if this is still the latest translation task
            if pending_translation_task and not pending_translation_task.cancelled():
                if text.strip():
                    logger.info(f"⏰ DELAYED TRANSLATION of incomplete Arabic text: '{text}'")
                    await _translate_sentences([text])
                pending_translation_task = None
        except asyncio.CancelledError:
            logger.debug("Translation task was cancelled (newer transcript received)")

    async def _forward_transcription(
        stt_stream: stt.SpeechStream,
        track: rtc.Track,
    ):
        """Forward the transcription and log the transcript in the console"""
        nonlocal accumulated_text, last_final_transcript, pending_translation_task
        
        try:
            async for ev in stt_stream:
                # Log to console for interim (word-by-word)
                if ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                    print(ev.alternatives[0].text, end="", flush=True)
                    
                    # Publish interim transcription for real-time word-by-word display
                    interim_text = ev.alternatives[0].text.strip()
                    if interim_text:
                        try:
                            interim_segment = rtc.TranscriptionSegment(
                                id=utils.misc.shortuuid("SG_"),
                                text=interim_text,
                                start_time=0,
                                end_time=0,
                                language=source_language,  # Arabic
                                final=False,  # This is interim, not final
                            )
                            interim_transcription = rtc.Transcription(
                                job.room.local_participant.identity, "", [interim_segment]
                            )
                            await job.room.local_participant.publish_transcription(interim_transcription)
                        except Exception as e:
                            logger.debug(f"Failed to publish interim transcription: {str(e)}")
                    
                elif ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    print("\n")
                    final_text = ev.alternatives[0].text.strip()
                    print(" -> ", final_text)
                    logger.info(f"Final Arabic transcript: {final_text}")

                    if final_text and final_text != last_final_transcript:
                        last_final_transcript = final_text
                        
                        # Publish final transcription for the original language (Arabic)
                        try:
                            final_segment = rtc.TranscriptionSegment(
                                id=utils.misc.shortuuid("SG_"),
                                text=final_text,
                                start_time=0,
                                end_time=0,
                                language=source_language,  # Arabic
                                final=True,
                            )
                            final_transcription = rtc.Transcription(
                                job.room.local_participant.identity, "", [final_segment]
                            )
                            await job.room.local_participant.publish_transcription(final_transcription)
                            
                            # Also broadcast Arabic transcription to WebSocket display clients
                            asyncio.create_task(broadcast_to_displays("transcription", source_language, final_text))
                            
                            logger.info(f"✅ Published final {languages[source_language].name} transcription: '{final_text}'")
                        except Exception as e:
                            logger.error(f"❌ Failed to publish final transcription: {str(e)}")
                        
                        # Handle translation logic
                        if translators:
                            # SIMPLE ACCUMULATION LOGIC - ONLY APPEND, NEVER REPLACE
                            if accumulated_text:
                                # ALWAYS append new final transcript to existing accumulated text
                                accumulated_text = accumulated_text.strip() + " " + final_text
                            else:
                                # First transcript - start accumulation
                                accumulated_text = final_text
                            
                            logger.info(f"📝 Updated accumulated Arabic text: '{accumulated_text}'")
                            
                            # Extract complete sentences from accumulated text
                            complete_sentences, remaining_text = _extract_complete_sentences(accumulated_text)
                            
                            if complete_sentences:
                                # We have complete sentences - translate them immediately
                                logger.info(f"🎯 Found {len(complete_sentences)} complete Arabic sentences: {complete_sentences}")
                                
                                # Cancel any pending translation
                                if pending_translation_task:
                                    pending_translation_task.cancel()
                                    pending_translation_task = None
                                
                                # Translate complete sentences
                                await _translate_sentences(complete_sentences)
                                
                                # Update accumulated text to only remaining incomplete text
                                accumulated_text = remaining_text
                                logger.info(f"📝 Updated accumulated Arabic text after sentence extraction: '{accumulated_text}'")
                            
                            # Handle remaining incomplete text with shorter delay
                            if accumulated_text.strip():
                                logger.info(f"📝 Incomplete Arabic text remaining, setting up delayed translation: '{accumulated_text}'")
                                
                                # Cancel any previous pending translation
                                if pending_translation_task:
                                    pending_translation_task.cancel()
                                
                                # Set up new delayed translation for incomplete text
                                pending_translation_task = asyncio.create_task(
                                    _delayed_translation(accumulated_text, translation_delay)
                                )
                            else:
                                # No remaining text - cancel any pending translation
                                if pending_translation_task:
                                    pending_translation_task.cancel()
                                    pending_translation_task = None
                        else:
                            logger.warning(f"⚠️ No translators available in room {job.room.name}, only {languages[source_language].name} transcription published")
                    else:
                        logger.debug("Empty or duplicate transcription, skipping")
        except Exception as e:
            logger.error(f"STT transcription error: {str(e)}")
            raise

    async def transcribe_track(participant: rtc.RemoteParticipant, track: rtc.Track):
        try:
            logger.info(f"🎤 Starting Arabic transcription for participant {participant.identity}, track {track.sid}")
            audio_stream = rtc.AudioStream(track)
            stt_stream = stt_provider.stream()
            stt_task = asyncio.create_task(
                _forward_transcription(stt_stream, track)
            )
            tasks.append(stt_task)

            frame_count = 0
            async for ev in audio_stream:
                frame_count += 1
                if frame_count % 100 == 0:  # Log every 100 frames to avoid spam
                    logger.debug(f"🔊 Received audio frame #{frame_count} from {participant.identity}")
                stt_stream.push_frame(ev.frame)
                
            logger.warning(f"🔇 Audio stream ended for {participant.identity}")
        except Exception as e:
            logger.error(f"❌ Transcription track error for {participant.identity}: {str(e)}")
            raise

    @job.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.TrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        logger.info(f"🎵 Track subscribed: {track.kind} from {participant.identity} (track: {track.sid})")
        logger.info(f"Track details - muted: {publication.muted}")
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info(f"✅ Adding Arabic transcriber for participant: {participant.identity}")
            tasks.append(asyncio.create_task(transcribe_track(participant, track)))
        else:
            logger.info(f"❌ Ignoring non-audio track: {track.kind}")

    @job.room.on("track_published")
    def on_track_published(publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"📡 Track published: {publication.kind} from {participant.identity} (track: {publication.sid})")
        logger.info(f"Publication details - muted: {publication.muted}")

    @job.room.on("track_unpublished") 
    def on_track_unpublished(publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"📡 Track unpublished: {publication.kind} from {participant.identity}")

    @job.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"👥 Participant connected: {participant.identity}")

    @job.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        logger.info(f"👥 Participant disconnected: {participant.identity}")

    @job.room.on("participant_attributes_changed")
    def on_attributes_changed(
        changed_attributes: dict[str, str], participant: rtc.Participant
    ):
        """
        When participant attributes change, handle new translation requests.
        """
        logger.info(f"🌍 Participant {participant.identity} attributes changed: {changed_attributes}")
        lang = changed_attributes.get("captions_language", None)
        if lang:
            if lang == source_language:
                logger.info(f"✅ Participant {participant.identity} requested {languages[source_language].name} (source language - Arabic)")
            elif lang in translators:
                logger.info(f"✅ Participant {participant.identity} requested existing language: {lang}")
                logger.info(f"📊 Current translators for this room: {list(translators.keys())}")
            else:
                # Check if the language is supported and different from source language
                if lang in languages:
                    try:
                        # Create a translator for the requested language using the language enum
                        language_obj = languages[lang]
                        language_enum = getattr(LanguageCode, language_obj.name)
                        translators[lang] = Translator(job.room, language_enum)
                        logger.info(f"🆕 Added translator for ROOM {job.room.name} (requested by {participant.identity}), language: {language_obj.name}")
                        logger.info(f"📊 Total translators for room {job.room.name}: {len(translators)} -> {list(translators.keys())}")
                        logger.info(f"🔍 Translators dict ID: {id(translators)}")
                        
                        # Debug: Verify the translator was actually added
                        if lang in translators:
                            logger.info(f"✅ Translator verification: {lang} successfully added to room translators")
                        else:
                            logger.error(f"❌ Translator verification FAILED: {lang} not found in translators dict")
                            
                    except Exception as e:
                        logger.error(f"❌ Error creating translator for {lang}: {str(e)}")
                else:
                    logger.warning(f"❌ Unsupported language requested by {participant.identity}: {lang}")
                    logger.info(f"💡 Supported languages: {list(languages.keys())}")
        else:
            logger.debug(f"No caption language change for participant {participant.identity}")

    logger.info("Connecting to room...")
    await job.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"Successfully connected to room: {job.room.name}")
    logger.info(f"📡 Real transcription data will be sent to external WebSocket server on ws://localhost:8765")
    
    # Debug room state after connection
    logger.info(f"Room participants: {len(job.room.remote_participants)}")
    for participant in job.room.remote_participants.values():
        logger.info(f"Participant: {participant.identity}")
        logger.info(f"  Audio tracks: {len(participant.track_publications)}")
        for sid, pub in participant.track_publications.items():
            logger.info(f"    Track {sid}: {pub.kind}, muted: {pub.muted}")

    # Also check local participant
    logger.info(f"Local participant: {job.room.local_participant.identity}")
    logger.info(f"Local participant tracks: {len(job.room.local_participant.track_publications)}")

    @job.room.local_participant.register_rpc_method("get/languages")
    async def get_languages(data: rtc.RpcInvocationData):
        languages_list = [asdict(lang) for lang in languages.values()]
        return json.dumps(languages_list)


async def request_fnc(req: JobRequest):
    await req.accept(
        name="agent",
        identity="agent",
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, request_fnc=request_fnc
        )
    )