"""
Inference Scheduler
===================
Every `interval_sec` seconds, picks the most recent `frames_per_request`
snapshots from SnapshotWorker and sends them to a remote VLM server using
the OpenAI-compatible Chat Completions API with vision.

API endpoint used:  POST {base_url}/v1/chat/completions

Request (OpenAI vision format):
  {
    "model": "<model>",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "<prompt>"},
          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<b64>"}},
          ...  # one entry per frame
        ]
      }
    ]
  }

The assistant reply text is extracted from choices[0].message.content and
stored as {"text": "..."}. The full raw response is available as "_raw".
"""

import base64
import json
import logging
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class InferenceScheduler:
    """Periodically triggers VLM inference using the OpenAI Chat Completions API."""

    def __init__(
        self,
        snapshot_worker,
        result_manager,
        interval_sec: int = 3,
        frames_per_request: int = 2,
        base_url: str = "",
        api_key: str = "none",
        model: str = "llava",
        prompt: str = "Describe what you see in the image(s).",
        timeout_sec: int = 30,
    ) -> None:
        self._snapshots = snapshot_worker
        self._results = result_manager
        self._interval = interval_sec
        self._frames_per_request = frames_per_request
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._prompt = prompt
        self._timeout = timeout_sec
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._base_url:
            logger.warning("INFERENCE_API_URL is not set — inference disabled")
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(
            "Inference scheduler started (interval=%d s, frames=%d)",
            self._interval,
            self._frames_per_request,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("Inference scheduler stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Wait one full interval before the first trigger so the snapshot
        # buffer has time to accumulate frames.
        time.sleep(self._interval)
        while self._running:
            t0 = time.monotonic()
            if self._base_url:
                try:
                    self._trigger()
                except Exception as exc:
                    logger.error("Inference trigger error: %s", exc)
            elapsed = time.monotonic() - t0
            sleep_time = max(0.0, self._interval - elapsed)
            time.sleep(sleep_time)

    def _trigger(self) -> None:
        snaps = self._snapshots.get_recent(self._frames_per_request)
        if not snaps:
            logger.warning("No snapshots available for inference — skipping")
            return

        # Build the content array: text prompt followed by one image block per frame.
        content = [{"type": "text", "text": self._prompt}]
        for s in snaps:
            b64 = base64.b64encode(s.jpeg).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{self._base_url}/v1/chat/completions"

        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            raw = resp.json()
        except requests.RequestException as exc:
            logger.error("Inference API error: %s", exc)
            self._results.update_result({
                "error": str(exc),
                "_inferred_at": time.time(),
                "_frame_count": len(snaps),
            })
            return

        # Extract assistant reply text from the standard OpenAI response shape.
        try:
            reply_text = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            reply_text = None

        # The model is expected to return a JSON object.  Strip any markdown
        # code-fences (```json ... ```) that VLMs often add, then parse.
        parsed = self._parse_reply(reply_text)

        result = {
            **parsed,                          # status, action_advice, reason …
            "_reply_raw": reply_text,          # original text for debugging
            "_model": raw.get("model", self._model),
            "_inferred_at": time.time(),
            "_frame_count": len(snaps),
        }
        self._results.update_result(result)
        logger.info("Inference result: %s", str(parsed)[:120])

    @staticmethod
    def _parse_reply(text: str | None) -> dict:
        """Try to parse the model reply as JSON.  Returns {"error": ...} on failure."""
        if not text:
            return {"error": "Empty reply from model"}

        # Strip markdown code fences: ```json ... ``` or ``` ... ```
        clean = re.sub(r"^```[\w]*\n", "", text.strip(), flags=re.MULTILINE)
        clean = re.sub(r"```$", "", clean.strip(), flags=re.MULTILINE).strip()

        # Find the first {...} block in the text in case there is surrounding prose
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            clean = m.group(0)

        try:
            return json.loads(clean)
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse model reply as JSON: %s", exc)
            return {"error": f"Non-JSON reply: {text[:200]}"}
