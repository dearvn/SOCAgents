"""Skill evals: does a model follow a skill's playbook on a fixed market situation?

Each case is a hand-written market situation compiled from one official skill: the context the
Strategist would see, the option quotes it can look up, the outcome that is expected (an idea or
no trade), deterministic checks frozen before the reference answer was written, and a
Must-do / Must-avoid / Best-practice rubric taken from the skill.

Cases use synthetic data only, never member data. A run calls the Strategist role with the
production prompt, serves ``get_option_quote`` from the case, and scores the answer with the
checks. Nothing is stored in the desk database and no idea is ever risk-checked for execution.
"""
