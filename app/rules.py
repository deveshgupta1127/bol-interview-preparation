"""Deterministic rules for things that don't need a model: the same input always gives the same result.

  find_skills()     tools and skills named in a text, from a dictionary (resume skills, answer details)
  claim_strength()  a resume claim is "specific" when it gives a number or scale, else "vague"
  OPENERS           the fixed first question of each round
  specifics()       the 0-3 "specifics" score of an answer: something named, your own role, a number or result

Claude still writes the tailored questions, judges whether the answer fits the question and the job, and coaches.
"""
import re

# ---------- skills dictionary ----------
# (name shown, spellings, case-sensitive). Case-sensitive entries are ordinary English words in lower case
# ("react", "spring", "express", "go"), so only the capitalised spelling counts. Ambiguous one-letter names
# (C, R) and "Go" only count in unambiguous spellings.
_SKILLS: list[tuple[str, tuple[str, ...], bool]] = [
    # languages
    ("Python", ("Python",), False), ("Java", ("Java",), False), ("JavaScript", ("JavaScript", "JS"), False),
    ("TypeScript", ("TypeScript",), False), ("C++", ("C++", "CPP"), False), ("C#", ("C#", "C sharp"), False),
    ("C", ("C language", "C programming"), False), ("Go", ("Golang", "Go lang"), False), ("Rust", ("Rust",), True),
    ("Kotlin", ("Kotlin",), False), ("Swift", ("Swift",), True), ("PHP", ("PHP",), False), ("Ruby", ("Ruby",), True),
    ("R", ("R programming", "RStudio"), False), ("MATLAB", ("MATLAB",), False), ("Scala", ("Scala",), True),
    ("Dart", ("Dart",), True), ("SQL", ("SQL",), True), ("Bash", ("Bash", "Shell scripting"), False),
    ("HTML", ("HTML", "HTML5"), False), ("CSS", ("CSS", "CSS3"), False),
    # web
    ("React Native", ("React Native",), True), ("React", ("React", "React.js", "ReactJS"), True),
    ("Angular", ("Angular", "AngularJS"), True), ("Vue.js", ("Vue", "Vue.js", "VueJS"), True),
    ("Next.js", ("Next.js", "NextJS"), False), ("Node.js", ("Node.js", "NodeJS", "Node"), True),
    ("Express.js", ("Express.js", "ExpressJS"), False), ("Django", ("Django",), False), ("Flask", ("Flask",), True),
    ("FastAPI", ("FastAPI",), False), ("Spring Boot", ("Spring Boot", "Spring Framework"), False),
    ("Tailwind CSS", ("Tailwind",), False), ("Bootstrap", ("Bootstrap",), True), ("jQuery", ("jQuery",), False),
    ("REST APIs", ("REST", "RESTful", "REST API", "REST APIs"), True), ("GraphQL", ("GraphQL",), False),
    # data and AI
    ("MySQL", ("MySQL",), False), ("PostgreSQL", ("PostgreSQL", "Postgres"), False),
    ("MongoDB", ("MongoDB", "Mongo"), False), ("SQLite", ("SQLite",), False), ("Redis", ("Redis",), False),
    ("Firebase", ("Firebase",), False), ("Excel", ("Excel", "MS Excel"), True), ("Power BI", ("Power BI", "PowerBI"), False),
    ("Tableau", ("Tableau",), False), ("Pandas", ("Pandas",), False), ("NumPy", ("NumPy",), False),
    ("Matplotlib", ("Matplotlib",), False), ("scikit-learn", ("scikit-learn", "sklearn", "scikit learn"), False),
    ("TensorFlow", ("TensorFlow",), False), ("PyTorch", ("PyTorch",), False), ("Keras", ("Keras",), False),
    ("OpenCV", ("OpenCV",), False), ("Machine Learning", ("Machine Learning", "ML"), False),
    ("Deep Learning", ("Deep Learning",), False), ("NLP", ("NLP", "Natural Language Processing"), False),
    ("Computer Vision", ("Computer Vision",), False), ("LLMs", ("LLM", "LLMs", "Large Language Models"), False),
    ("Generative AI", ("Generative AI", "GenAI", "Gen AI"), False), ("Data Analysis", ("Data Analysis", "Data Analytics"), False),
    ("Data Structures", ("DSA", "Data Structures"), False), ("Statistics", ("Statistics",), False),
    ("Spark", ("Apache Spark", "PySpark", "Spark"), True), ("Hadoop", ("Hadoop",), False),
    # cloud and tools
    ("AWS", ("AWS", "Amazon Web Services"), False), ("Azure", ("Azure",), False), ("GCP", ("GCP", "Google Cloud"), False),
    ("Docker", ("Docker",), False), ("Kubernetes", ("Kubernetes", "K8s"), False), ("Git", ("Git",), False),
    ("GitHub", ("GitHub",), False), ("Linux", ("Linux",), False), ("Jenkins", ("Jenkins",), True),
    ("CI/CD", ("CI/CD", "CICD"), False), ("Terraform", ("Terraform",), False), ("Android", ("Android",), False),
    ("iOS", ("iOS",), True), ("Flutter", ("Flutter",), True), ("Figma", ("Figma",), False), ("Jira", ("Jira",), False),
    ("Postman", ("Postman",), True), ("Selenium", ("Selenium",), True), ("JUnit", ("JUnit",), False),
    ("Pytest", ("Pytest",), False), ("Agile", ("Agile",), True), ("Scrum", ("Scrum",), False),
    ("Arduino", ("Arduino",), False), ("Raspberry Pi", ("Raspberry Pi",), False), ("AutoCAD", ("AutoCAD",), False),
    ("SolidWorks", ("SolidWorks",), False), ("Tally", ("Tally", "Tally ERP", "Tally Prime"), True),
    ("SAP", ("SAP",), True), ("Salesforce", ("Salesforce",), False), ("Photoshop", ("Photoshop",), False),
    ("Canva", ("Canva",), False), ("WordPress", ("WordPress",), False), ("Unity", ("Unity",), True),
    # people skills
    ("Leadership", ("Leadership",), False), ("Communication", ("Communication",), False),
    ("Teamwork", ("Teamwork", "Team work"), False), ("Problem Solving", ("Problem Solving", "Problem-solving"), False),
    ("Public Speaking", ("Public Speaking",), False),
]


def _pattern(spellings: tuple[str, ...], case_sensitive: bool) -> re.Pattern:
    # Not inside a longer word ("Java" in "JavaScript", "Git" in "GitHub"), and not followed by + or # (C vs C++).
    alts = "|".join(re.escape(s).replace(r"\ ", r"[\s-]?") for s in sorted(spellings, key=len, reverse=True))
    return re.compile(rf"(?<![\w.+#/-])(?:{alts})(?![\w+#]|\.\w)", 0 if case_sensitive else re.I)


_SKILL_PATTERNS = [(name, _pattern(spellings, cs)) for name, spellings, cs in _SKILLS]


def find_skills(text: str, limit: int = 20) -> list[str]:
    """Skills named in the text, in the order they first appear."""
    found = []
    for name, pattern in _SKILL_PATTERNS:
        for m in pattern.finditer(text or ""):
            found.append((m.start(), -m.end(), name))
    names: list[str] = []
    covered = -1  # a match inside a longer one ("React" in "React Native") doesn't count separately
    for start, neg_end, name in sorted(found):
        if start < covered:
            continue
        covered = -neg_end
        if name not in names:
            names.append(name)
    return names[:limit]


# ---------- numbers, scale and results ----------
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_NUMBER = re.compile(
    r"\d|[०-९]|%|\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|lakhs?|crores?|percent|double[ds]?|twice|half)\b"
    r"|सौ|हज़ार|हजार|लाख|करोड़|प्रतिशत", re.I)
# Outcomes that happened (past tense), not hopes: "we cut the queue", not "I want to grow my career".
_RESULT = re.compile(r"\b(reduced|increased|improved|saved|cut down|grew|boosted|won|ranked|launched|deployed|"
                     r"adopted|awarded|shipped|first prize|second prize|third prize|came down|went up|faster|fewer)\b",
                     re.I)


def has_number(text: str) -> bool:
    """A count, amount, percentage or scale. Graduation years like 2026 don't count."""
    return bool(_NUMBER.search(_YEAR.sub(" ", text or "")))


def claim_strength(text: str) -> str:
    """Resume claims with a number or scale are "specific"; the rest are "vague" (Bol asks hardest about those)."""
    return "specific" if has_number(text) else "vague"


# ---------- the opening question of each round ----------
OPENERS = {
    "hr": ("Tell me about yourself.",
           "Checks how clearly you introduce yourself and what you choose to highlight."),
    "technical": ("Walk me through the project you are most proud of.",
                  "Checks how well you explain your own work, from the problem to the result."),
    "behavioural": ("Tell me about a time you solved a difficult problem in a team.",
                    "Checks whether you can tell a clear story: the situation, what you did, and the result."),
    "campus": ("Introduce yourself in one minute.",
               "Checks whether you can give a short, confident introduction."),
}


# ---------- the "specifics" score of an answer ----------
_OWN_ROLE = re.compile(
    r"\b(I|we)\s+(?:\w+\s+)?(built|led|lead|designed|developed|created|implemented|wrote|coded|made|deployed|managed|"
    r"handled|owned|tested|fixed|trained|automated|analy[sz]ed|organi[sz]ed|set up|integrated|optimi[sz]ed|"
    r"researched|presented|mentored|coordinated|planned|launched|solved|debugged|worked|interned|volunteered|contributed)\b"
    r"|\bmy (role|part|responsibility|job) (was|is)\b|\bI was responsible\b|मैंने|हमने", re.I)
# Names: capitalised words that don't start a sentence (TCS, Smart India Hackathon, Infosys).
_NAME = re.compile(r"(?<![.!?।]\s)(?<!^)\b(?!I\b|I'm\b|I've\b|I'd\b|OK\b|(?:January|February|March|April|May|June|"
                   r"July|August|September|October|November|December|Monday|Tuesday|Wednesday|Thursday|Friday|"
                   r"Saturday|Sunday|English|Hindi)\b)[A-Z][\w&+#.-]*[A-Za-z0-9+#]")
MIN_WORDS = 8


def specifics(answer: str) -> tuple[int, str]:
    """0-3: one point each for naming something concrete, saying what you did, and giving a number or result."""
    text = answer or ""
    if len(text.split()) < MIN_WORDS:
        return 0, "Too short to show any specifics."
    tools = find_skills(text, limit=4)
    named = tools or [m.group(0) for m in _NAME.finditer(text)][:3]
    role = _OWN_ROLE.search(text)
    measure = has_number(text) or _RESULT.search(text)
    score = (1 if named else 0) + (1 if role else 0) + (1 if measure else 0)

    said = []
    if named:
        said.append("named " + ", ".join(named))
    if role:
        said.append("said what you did")
    if measure:
        said.append("gave a number or result")
    missing = [] if named else ["name the tool, project or company"]
    if not role:
        missing.append('say what you did ("I built...")')
    if not measure:
        missing.append("add a number or result")
    parts = []
    if said:
        parts.append("You " + "; ".join(said) + ".")
    if missing:
        parts.append("Next time " + " and ".join(missing) + ".")
    return score, " ".join(parts)
