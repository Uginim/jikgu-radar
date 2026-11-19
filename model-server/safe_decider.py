"""Completion-checked adapter for the pinned HF Zoro-1 Decider.

Prompt/image preparation follows trillionlabs/Zoro-1 bf1b76b's think method
(Apache-2.0). Unlike that method, retain generated stop-token evidence before
special-token decoding. No weights or upstream checkpoint files are modified.
"""
import time

import torch
from torch.nn.attention import SDPBackend, sdpa_kernel
from zoro_decide import Decider, END_THINK
from decide_request import MAX_IMAGE_PIXELS, VISION, image_input, parse, prompt, with_reasoning


def completion_stats(tokens, stop_ids, budget):
    """Only a generated terminator proves completion, even at the budget boundary."""
    count = int(tokens.shape[0])
    stopped = count > 0 and int(tokens[-1]) in stop_ids
    return {"finish_reason": "stop" if stopped else "length" if count >= budget else "unknown",
            "completion_tokens": count}


class SafeDecider(Decider):
    def _think_checked(self, request, max_new_tokens, image=None, temperature=0.6, seed=None):
        if image is None:
            text = self.tok.apply_chat_template([{"role": "user", "content": prompt(request)}], tokenize=False,
                                                add_generation_prompt=True, enable_thinking=True)
            ids = self.tok(text, return_tensors="pt").input_ids.to(self.device)
            inputs = {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
        else:
            pixel_values, grid, n = image_input(image, self.proc, MAX_IMAGE_PIXELS, None)
            messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt(request)}]}]
            text = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=True)
            text = text.replace(VISION[1], VISION[1] * n, 1)
            ids = self.tok(text, return_tensors="pt").input_ids.to(self.device)
            inputs = {"input_ids": ids, "attention_mask": torch.ones_like(ids),
                      "pixel_values": pixel_values.to(self.device, self.model.dtype), "image_grid_thw": grid.to(self.device),
                      "mm_token_type_ids": (ids == self.image_pad).int()}
        if seed is not None:
            torch.manual_seed(seed)
        with sdpa_kernel([SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION, SDPBackend.MATH]):
            out = self.lm.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=temperature > 0,
                                   temperature=temperature or None, top_p=0.95, top_k=20, eos_token_id=self.stops)
        generated = out[0, ids.shape[1]:]
        stats = completion_stats(generated, self.stops, max_new_tokens)
        text = self.tok.decode(generated, skip_special_tokens=True).replace(END_THINK, "").strip()
        return text, stats

    def decide(self, request, image=None, temperature=None, reasoning=False, max_reasoning_tokens=4096,
               threshold=0.7, seed=None):
        t0 = time.perf_counter()
        img = parse(request, image).image
        extra = {"answers_source": "direct"}
        direct, keep = None, {}
        if reasoning == "auto":
            direct, top = self._direct(request, image, temperature)
            unsure = [qid for qid, p in zip(direct, top) if p < threshold]
            extra["escalated"] = unsure
            if not unsure:
                return {"answers": direct, "timing_ms": round((time.perf_counter() - t0) * 1000, 1), **extra}
            keep = {qid: a for qid, a in direct.items() if qid not in unsure}
        if reasoning:
            t_think = time.perf_counter()
            text, stats = self._think_checked(request, max_reasoning_tokens, image=img, seed=seed)
            extra.update(reasoning=text, reasoning_stats=stats,
                         reasoning_ms=round((time.perf_counter() - t_think) * 1000, 1))
            if stats.get("finish_reason") != "stop":
                extra["cut"] = "length"
            elif not text.strip():
                extra["cut"] = "empty"
            else:
                request = with_reasoning(request, text)
                extra["answers_source"] = "reasoned"
        # A cut must never feed the partial reasoning into the decision head.
        if "cut" in extra and direct is not None:
            answers = direct
        else:
            answers, _ = self._direct(request, image, temperature)
            answers.update(keep)
        return {"answers": answers, "timing_ms": round((time.perf_counter() - t0) * 1000, 1), **extra}
