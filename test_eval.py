from ai_evaluator import evaluate_job_match, load_cv

# 1. Verify CV Loading
print("--- Checking CV Load ---")
cv_text = load_cv()
if not cv_text:
    print("ERROR: Could not load CV. Check that a resume/CV PDF or TXT file exists in this folder.")
else:
    print(f"Successfully loaded CV! Length: {len(cv_text)} characters.\n")

# 2. Define a Real-Life Job Description sample (e.g., a Senior Project Manager role)
sample_jd = """
Role: Senior Technical Project Manager
Location: Mumbai, Maharashtra, India
Experience Required: 8-12 Years
Description:
We are looking for a seasoned Senior Technical Project Manager to lead cross-functional software delivery teams.
Responsibilities include managing project lifecycles, Agile/Scrum ceremonies, stakeholder management, resource planning, and risk mitigation.
Requirements:
- 8+ years of total experience in IT/Software project and program management.
- Strong proficiency in Agile, Scrum, PMP or PRINCE2 frameworks.
- Excellent stakeholder communication skills and experience managing enterprise client portfolios.
- Experience coordinating technical teams across global time zones.
"""

# 3. Run the AI Evaluation
print("--- Running Gemini AI Match Evaluation ---")
evaluation_result = evaluate_job_match(sample_jd, cv_text)

print("\n--- FULL RESULT ---")
for key, value in evaluation_result.items():
    print(f"{key}: {value}")

if evaluation_result.get("error"):
    print("\n--- THIS IS THE PART TO SHARE BACK ---")
    print("Full raw error text:")
    print(evaluation_result["error"])