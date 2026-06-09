import os
from google import genai

# 1. Initialize the client (Your exact code snippet)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.0-flash"

try:
    # 2. Make a simple test generation call
    response = client.models.generate_content(
        model=MODEL,
        contents="Say 'API connection successful!'",
    )
    
    # 3. Print the output
    print("--- Success ---")
    print(response.text)

except Exception as e:
    print("--- Error Connection Failed ---")
    print(e)
