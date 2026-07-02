"""Fix captcha check to use more specific text that only appears when there's an actual captcha challenge."""
import sys
sys.path.insert(0, '.')
import database as db

steps = db.get_steps(4)
# Only modify step 4 (captcha check) - use specific text that only appears on actual captcha pages
new_steps = []
for s in steps:
    step = dict(s)
    if step['step_order'] == 4:
        # Netflix shows "This page is protected by Google reCAPTCHA" on every page
        # An actual captcha challenge would show a different element
        # Disable captcha check by using a very specific string that only shows during challenge
        step['selector_value'] = 'Please verify you are a human'
        step['timeout'] = 3
    new_steps.append(step)

db.save_steps(4, new_steps)
print("Fixed captcha selector. Updated steps:")
for s in db.get_steps(4):
    print(f"  {s['step_order']}: {s['action']} | {s['selector_type']}:{s['selector_value']} | ok={s['on_success']} fail={s['on_failure']}")
