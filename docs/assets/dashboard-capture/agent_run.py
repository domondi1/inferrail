from openai import APIStatusError, OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused", default_headers={
    "X-Inferrail-Attribute-Customer": "acme",
    "X-Inferrail-Attribute-Work-Id": "contract-review-42",
    "X-Inferrail-Budget-Usd": "0.04",
})
contract = open("contract.txt").read()

for step in ["extract clauses", "check liability", "check renewal",
             "compare to playbook", "draft summary", "draft reply"]:
    try:
        client.chat.completions.create(model="gpt-4o", max_tokens=400, messages=[
            {"role": "user", "content": f"{step}:\n{contract}"}])
        print(f"{step:<20} answered")
    except APIStatusError as e:
        print(f"{step:<20} refused {e.status_code}, provider not called")
