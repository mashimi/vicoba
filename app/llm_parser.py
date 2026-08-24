"""Swahili NLU Parser with Local Engine (Cactus / Ollama) & Offline Rule Fallback.

Same interface as parser.parse(): Swahili text in, ParsedIntent out.
Supports local OpenAI-compatible inference servers (http://localhost:11434/v1 or http://localhost:8080/v1).
If the local server is offline or unreachable, silently falls back to parser.parse() (rule-based).
"""
import json
import os
import httpx

from . import config
from .parser import ParsedIntent, parse as rule_parse

SYSTEM_PROMPT = """\
You are a Swahili-language NLU parser for a VICOBA (Village Community Bank) treasurer app.
Given a single user message, return ONLY a JSON object with this exact schema (no other text):

{
  "action": "register" | "contribute" | "fee" | "fine" | "loan" | "repay" | "payout" | "exit" | "member_statement" | "who_unpaid" | "group_position" | "unknown",
  "member": "full name or null",
  "member_no": "BSDA-001 or null",
  "amount": 0,
  "amounts": {"hisa": 5000, "jamii": 1000, "bima": 2000} or null,
  "loan_id": null,
  "guarantors": ["name1", "name2"] or null,
  "mpesa_ref": "QX84920193 or null",
  "description": "short Swahili summary"
}

Rules:
- Parse amounts in whole Tanzanian shillings (TSH). Ignore commas/spaces in numbers.
- "hisa" = share purchase (member equity), "jamii" = social fund, "bima" = insurance fund.
- A bare number after a name with no category label = hisa by default.
- Member names may have two parts (first + last). Preserve them.
- Extract M-Pesa transaction reference (e.g. QX84920193) into mpesa_ref if present.
- Return valid JSON only — no markdown fences, no explanation.
"""


async def parse(text: str) -> ParsedIntent:
    provider = config.llm_provider()
    try:
        if provider == "anthropic" and config.anthropic_key():
            return await _parse_anthropic(text)
        elif provider in ("local", "cactus"):
            return await _parse_local(text)
    except Exception as e:
        # Silent offline fallback to zero-dependency rule parser
        pass
    return rule_parse(text)


async def _parse_local(text: str) -> ParsedIntent:
    url = config.local_llm_url()
    model = config.local_llm_model()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        return ParsedIntent(**{k: parsed.get(k) for k in ParsedIntent.__dataclass_fields__})


async def _parse_anthropic(text: str) -> ParsedIntent:
    key = config.anthropic_key()
    model = config.llm_model()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            },
        )
        resp.raise_for_status()
        block = resp.json()["content"][0]
        raw = block["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        return ParsedIntent(**{k: parsed.get(k) for k in ParsedIntent.__dataclass_fields__})
