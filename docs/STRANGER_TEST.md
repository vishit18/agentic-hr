# Stranger test

Goal: someone who has never seen the app does real tasks with no help. If they get stuck, the screen needs fixing, not the person.

## Setup (2 minutes)
- App running (`docker compose up -d`), signed out. Give them a recruiter login. Use a phone for half the tasks if you can.
- Tell them only: "You work in HR. Use this to do the tasks on the card. Say out loud what you expect before you click. I will not help."
- You watch and write down: where they hesitate, what they click first, any wrong turn, any words they do not understand.

## Task card (hand this over, one task at a time)
1. **Sign in** and say what you think the first screen is asking you to do.
2. **Find a candidate** who has an accountancy or finance background and open their profile. What can you tell about them in 30 seconds?
3. **A company emailed this:** "We need a senior accountant in Manama, CFA preferred, at least 5 years, start in January." Add it as a new hiring request.
4. **Open the shortlist** that was made for it. Pick the person you would put first and say why, using only what the screen tells you.
5. **Remove** one person from the shortlist, then change your mind and **get them back**.
6. **Approve and send** the shortlist to the company. Where do you expect the email to have gone?
7. **Something is wrong:** two profiles look like the same person. Find out what the app suggests and decide.
8. **Something is broken:** pretend a page shows wrong data. Report it so an engineer can look.
9. **On a phone:** find a candidate and open a request.

## Scoring (per task)
| Mark | Meaning |
|------|---------|
| Done alone | finished with no hesitation over 10 seconds |
| Done, slowly | finished, but hesitated, backtracked or misread a label |
| Needed help | you had to step in (write down exactly what you said) |
| Failed | gave up or did the wrong thing |

## What to look for
- Words they did not understand (rename them)
- Buttons they did not notice or expected somewhere else
- Whether they understood that nothing leaves the building until they approve
- Whether they trusted the explanation text or went looking for the CV
- Anything that felt slow on the phone

## After the test
Fix the top three problems, run the tests (`docker compose run --rm api pytest -q tests`), and test again with a different person. Record results in the table below.

| Date | Tester | Done alone | Done slowly | Needed help | Failed | Top problems |
|------|--------|-----------|-------------|-------------|--------|--------------|
|      |        |           |             |             |        |              |
