"""
Calm by Design - grounding practice content
=============================================
Mirrors the content already loaded into the Airtable "Buffer Protocols"
table (source: Grounding_Practices_Content.md) so the Flask result page can
render a practice instantly without an extra Airtable round trip. All five
elements are represented - Earth, Air, Fire, Water, Spirit.
"""

SAFETY_FOOTER = (
    "If something feels medically urgent, unusually severe, or unsafe, stop "
    "this practice and seek immediate help. Call 911 for an emergency. If you "
    "may harm yourself or cannot remain safe, call or text 988 in the United States."
)

ELEMENTS = {
    "Earth": {
        "title": "The Five-Point Return",
        "trait": "Groundedness, Safety",
        "imbalance": "Disconnection, Fatigue",
        "duration": "2-3 minutes",
        "intro": "You do not have to push through this moment. Let the ground hold some of the weight for you.",
        "steps": [
            "Place both feet on the floor, or notice where your body is supported.",
            "Gently press down through five points: your left foot, right foot, left hand, right hand, and the place beneath your body.",
            "Look around and name three solid things you can see.",
            "Say quietly, “I am here. I am supported. I only need to take the next small step.”",
            "Stay where you are for three easy breaths. Let each exhale be natural, without forcing it.",
        ],
        "close": "Nothing needs to be solved right now. Return when your body feels ready, even if that means resting first.",
    },
    "Water": {
        "title": "Let the Wave Pass",
        "trait": "Emotional Flow",
        "imbalance": "Overwhelm, Edginess",
        "duration": "2-4 minutes",
        "intro": "This feeling can move through you without carrying you away.",
        "steps": [
            "Place one hand over your heart and the other over your lower belly, or wherever touch feels comforting.",
            "Notice the feeling without explaining it. Name it with one simple word if you can.",
            "Slowly sway, rock, or move your shoulders in a gentle rhythm.",
            "Imagine the feeling as a wave: rising, cresting, and beginning to soften.",
            "Take a sip of water and notice its temperature as you swallow.",
        ],
        "close": "You do not have to stop the wave. Let it move at its own pace while you stay beside yourself.",
    },
    "Fire": {
        "title": "Bank the Flame",
        "trait": "Motivation, Drive",
        "imbalance": "Anger, Burnout",
        "duration": "2-3 minutes",
        "intro": "There is a great deal of heat here. You do not have to act from it or bury it.",
        "steps": [
            "Press your palms firmly together for five seconds, then release. Repeat three times.",
            "Push both feet into the floor and notice the strength in your legs.",
            "Breathe out through your mouth as though you are gently cooling something hot.",
            "Unclench your jaw, lower your shoulders, and open your hands.",
            "Ask yourself, “What needs protection, and what can wait?”",
        ],
        "close": "Your fire is not the enemy. Let it become warmth and clarity before you decide what comes next.",
    },
    "Air": {
        "title": "Gather the Wind",
        "trait": "Clarity, Communication",
        "imbalance": "Scattered, Overthinking",
        "duration": "2-3 minutes",
        "intro": "Your mind may be moving in many directions. You only need to return to this one moment.",
        "steps": [
            "Look around and name five things you can see.",
            "Notice three sounds, including the quietest one you can hear.",
            "Let your next exhale last slightly longer than your inhale. Do not strain or hold your breath.",
            "Place every thought into one of two groups: “Now” or “Not now.”",
            "Choose one true sentence: “Right now, I am sitting.” “Right now, I am breathing.” “Right now, I am safe enough to pause.”",
        ],
        "close": "You do not have to follow every thought. Let the unnecessary ones pass like wind through an open window.",
    },
    "Spirit": {
        "title": "The Inner Light",
        "trait": "Wholeness, Intuition",
        "imbalance": "Lost, Detached",
        "duration": "3-5 minutes",
        "intro": "You may feel far away from yourself, but the path back is still here.",
        "steps": [
            "Keep your eyes open and gently notice where you are.",
            "Say your name, the day, and the place you are in.",
            "Place a hand over your heart or feel for your pulse at your wrist.",
            "Notice one thing your body feels, one thing your heart needs, and one thing that remains true.",
            "Say quietly, “The tunnel is dark because I am the light. I am still here. I still shine.”",
        ],
        "close": "You do not have to feel completely whole to begin returning. Follow the smallest flicker of yourself home.",
    },
}

ELEMENT_ROTATION = ["Earth", "Air", "Fire", "Water", "Spirit"]


def next_element(suggested, last_used):
    """Avoid automatically repeating the participant's last-used element
    when another suitable option exists. `suggested` is Gemini's (or the
    fallback mapping's) raw pick; if it matches the last element used,
    move to the next element in a fixed rotation instead."""
    if not suggested:
        suggested = ELEMENT_ROTATION[0]
    if suggested != last_used:
        return suggested
    idx = ELEMENT_ROTATION.index(suggested) if suggested in ELEMENT_ROTATION else 0
    return ELEMENT_ROTATION[(idx + 1) % len(ELEMENT_ROTATION)]
