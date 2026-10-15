# ==============================================================================
# All-In-One Kaggle Setup: DeepSeek-R1-Distill-Qwen-7B + FastAPI + Cloudflare
# ==============================================================================
import os
import re
import subprocess
import sys
import time

print(">>> [1/5] Installing dependencies & cloudflared...")
# Ensure clean PyTorch/CUDA environment
subprocess.run(["pip", "install", "-q", "fastapi", "uvicorn", "bitsandbytes", "accelerate", "transformers", "pydantic"], check=True)

# Download cloudflared binary if not present
if not os.path.exists("cloudflared"):
    subprocess.run(["wget", "-q", "-nc", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", "-O", "cloudflared"], check=True)
    subprocess.run(["chmod", "+x", "cloudflared"], check=True)

print(">>> [2/5] Loading DeepSeek-R1-Distill-Qwen-7B (4-bit NF4 on Tesla T4)...")
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

MODEL_ID = "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
)

tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    quantization_config=bnb_config,
    device_map="auto",
    torch_dtype=torch.float16,
)

print(">>> [3/5] Setting up FastAPI Server...")
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List, Union
import json

app = FastAPI(title="DeepSeek-R1 Gatekeeper Server")

class ValidationRequest(BaseModel):
    ticket: Optional[str] = None
    subgoal: Optional[str] = None
    scope: Optional[Union[str, List[str]]] = None
    diff: str = ""
    mechanical_detail: Optional[str] = ""
    investigation_notes: Optional[str] = ""
    is_final_verification: bool = False

@app.get("/health")
def health():
    return {"status": "healthy", "model": MODEL_ID, "gpu": torch.cuda.get_device_name(0)}

@app.post("/validate")
def validate(req: ValidationRequest):
    t0 = time.time()
    target_desc = req.ticket if req.is_final_verification else req.subgoal
    target_type = "Final Ticket Verification" if req.is_final_verification else "Subgoal Validation"
    scope_str = ", ".join(req.scope) if isinstance(req.scope, list) else (req.scope or "Not specified")
    
    prompt = (
        "You are an authoritative code gatekeeper and semantic diff validator.\n"
        "Evaluate whether the code changes (git diff) satisfy the target task without violating quality or safety constraints.\n\n"
        f"Target ({target_type}): {target_desc}\n"
        f"Scope: {req.scope or 'Not specified'}\n"
        f"Mechanical/Test Output: {req.mechanical_detail or 'None'}\n"
        f"Investigation Notes: {req.investigation_notes or 'None'}\n\n"
        "Git Diff:\n"
        "```diff\n"
        f"{req.diff}\n"
        "```\n\n"
        "Instructions:\n"
        "1. Reason step-by-step inside <think> tags. Check if the diff actually implements the required behavior, has syntax errors, misses imports, or is completely empty.\n"
        "2. At the end of your response, provide your verdict in a JSON code block with the exact keys:\n"
        "```json\n"
        "{\n"
        '  "valid": true or false,\n'
        '  "probability": float between 0.0 and 1.0,\n'
        '  "violations": ["list of strings if any"],\n'
        '  "reason": "concise explanation"\n'
        "}\n"
        "```"
    )

    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=1024,
            temperature=0.6,
            top_p=0.95,
            do_sample=True,
        )
    
    raw_response = tokenizer.decode(output_ids[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
    duration_ms = (time.time() - t0) * 1000

    # Extract CoT thinking
    cot = ""
    if "<think>" in raw_response and "</think>" in raw_response:
        cot = raw_response.split("<think>")[1].split("</think>")[0].strip()
    elif "</think>" in raw_response:
        cot = raw_response.split("</think>")[0].strip()

    # Extract JSON verdict
    valid = False
    prob = 0.5
    violations = []
    reason = "Failed to parse model verdict"
    
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_response, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            valid = bool(parsed.get("valid", False))
            prob = float(parsed.get("probability", 0.7))
            violations = list(parsed.get("violations", []))
            reason = str(parsed.get("reason", ""))
        except Exception:
            pass
    else:
        # Fallback check
        if '"valid": true' in raw_response.lower():
            valid = True
            prob = 0.8
            reason = "Model approved change"
        else:
            reason = raw_response[-200:].strip()

    return {
        "valid": valid,
        "probability": prob,
        "reason": reason,
        "violations": violations,
        "cot": cot,
        "duration_ms": duration_ms
    }

print(">>> [4/5] Launching FastAPI in background...")
import uvicorn
import threading

def run_api():
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

server_thread = threading.Thread(target=run_api, daemon=True)
server_thread.start()
time.sleep(3)

print(">>> [5/5] Launching Cloudflare Tunnel...")
cf_proc = subprocess.Popen(
    ["./cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True
)

public_url = None
start_time = time.time()
while time.time() - start_time < 30:
    line = cf_proc.stdout.readline()
    if not line:
        break
    m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
    if m:
        public_url = m.group(0)
        break

print("\n" + "="*70)
if public_url:
    print(f"🚀 YOUR LIVE DEEPSEEK-R1 ENDPOINT IS:\n\n   {public_url}\n")
    print("="*70)
else:
    print("❌ Tunnel timed out. Check Kaggle internet connection.")

# Keep cell alive with active heartbeat
print(">>> Server & Tunnel are active! (Keep this Kaggle cell running)")
minutes = 0
while True:
    time.sleep(60)
    minutes += 1
    if minutes % 5 == 0:
        print(f"[Keep-Alive] Running for {minutes} min. Endpoint: {public_url}")
