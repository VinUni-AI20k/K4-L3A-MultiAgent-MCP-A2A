import torch
from transformers import pipeline

model_id = "meta-llama/Llama-3.1-8B-Instruct"

# Khởi tạo pipeline
pipe = pipeline(
    "text-generation",
    model=model_id,
    model_kwargs={"torch_dtype": torch.bfloat16},
    device_map="auto",  # Tự động phân bổ vào GPU/CPU
)

messages = [
    {"role": "system", "content": "Bạn là một trợ lý AI hữu ích."},
    {
        "role": "user",
        "content": "Xin chào, hãy giới thiệu ngắn về bản thân bạn!",
    },
]

outputs = pipe(messages, max_new_tokens=256)
print(outputs[0]["generated_text"][-1]["content"])
