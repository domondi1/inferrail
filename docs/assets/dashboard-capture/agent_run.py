from openai import APIStatusError, OpenAI

job = {
    "X-Inferrail-Attribute-Work-Id": "contract-review-42",  # one AI job
    "X-Inferrail-Budget-Usd": "0.04",  # its spending limit, in dollars
}
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused", default_headers=job)
contract = open("contract.txt").read()

steps = ["extract clauses", "check liability", "check renewal",
         "compare to playbook", "draft summary", "draft reply"]
for step in steps:
    try:
        client.chat.completions.create(model="gpt-4o", max_tokens=400, messages=[
            {"role": "user", "content": f"{step}:\n{contract}"}])
        print(f"{step:<20} answered")
    except APIStatusError as e:
        print(f"{step:<20} blocked ({e.status_code}): over budget, not sent to the model")
