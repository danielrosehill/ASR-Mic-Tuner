"""Reading passages for the mic test, by length.

Lengths assume about 150 words a minute, a relaxed dictation pace. Each text
is written to exercise what speech recognition trips over: plosives (p, b,
t, k) that pop a close mic, sibilants (s, sh, ch) that a bright mic or a
de-esser mangles, soft word endings that a gate or VAD clips, numbers, dates,
names, and questions that change pitch. All original text, written for this
project (2026-10-27).
"""

# (id, title, seconds, text)
PASSAGES = [
    ("30-notes", "Morning notes", 30,
     "Good morning. Before the stand-up, a few quick notes. The printer on the second floor "
     "is broken again, so please send everything to the shared drive instead. Peter booked "
     "the big room for Tuesday at half past two. Should we push the budget review to Friday? "
     "I think so. Finally, the parcel from Basel arrived, but the box was soft and slightly "
     "crushed, so check the contents carefully."),
    ("30-shopping", "Errands", 30,
     "Remind me to pick up six bottles of sparkling water, fresh basil, sharp cheddar, and "
     "two packets of spaghetti. Pay the electricity bill before the fourteenth. Book the "
     "dentist for next Thursday, ideally in the afternoon. Call the bike shop about the "
     "back brake; it squeaks when the pads touch the disc. And buy a birthday card for "
     "Sasha, something simple, not too sentimental."),
    ("60-email", "An email", 60,
     "Hi Priya, thanks for sending the draft so quickly. I've read it twice and I think the "
     "structure works, but the second section needs tightening. The point about shipping "
     "costs is buried in a long paragraph, and it's the strongest argument we have. Could you "
     "move it up and add the figures from March? From memory, we spent about four thousand "
     "two hundred on freight, which is roughly thirty percent more than budgeted. Also, "
     "please double-check the spelling of Szczecin and Kraków in the appendix; the last "
     "version had both wrong. I'm out on Wednesday and Thursday, so if anything urgent comes "
     "up, speak to Ben or Charlotte. Otherwise, let's aim to send the final version to the "
     "board by the end of next week. Best wishes, and thanks again for the quick turnaround."),
    ("60-story", "A short story", 60,
     "The bus was late, as usual, so Martha stood under the shelter and watched the rain "
     "slide sideways across the square. A boy with a bright yellow backpack was trying to "
     "teach his small dog to sit. It would sit, briefly, then spring up and spin in a "
     "circle, barking at the pigeons. Behind them, the bakery switched on its lights, and "
     "the smell of warm bread drifted across the pavement. Martha checked her phone: "
     "seven forty-three. If the bus came in the next five minutes she would still make "
     "the eight fifteen train. She thought about the presentation, the slides she had "
     "rewritten at midnight, and the question she was dreading about the third quarter "
     "numbers. Then the bus appeared at the corner, splashing through a puddle, and the "
     "boy's dog finally, proudly, sat down."),
    ("120-report", "A project update", 120,
     "Project update for the week ending the twenty-fourth of October. Overall status is "
     "amber: most work is on track, but two risks need attention. First, the progress. The "
     "new booking system passed its acceptance tests on Monday, with three minor defects "
     "logged and two already fixed. The remaining one affects how bank holidays display in "
     "the calendar view, and Stefan expects a patch by Wednesday. Data migration is about "
     "eighty-five percent complete. We moved just over one point two million customer "
     "records without loss, and the checksums match. Second, the risks. The payment "
     "provider has changed its sandbox certificates without notice, which broke our "
     "automated tests for most of Thursday. We've asked for a named contact so it doesn't "
     "happen again. The other risk is staffing. Two people are on leave during the planned "
     "go-live week, so we either shift the date by seven days or bring in a contractor. My "
     "recommendation is to shift the date; it's cheaper and less disruptive. Third, the "
     "budget. We've spent sixty-two thousand of the eighty thousand allocated, so we're "
     "slightly ahead of plan, mostly because of the extra testing in September. Finally, "
     "the decisions I need from this group. Do we accept the one-week slip? And can someone "
     "from finance confirm whether the contractor option would come out of this budget or "
     "the general reserve? A few smaller items. The accessibility review found that two "
     "buttons on the checkout page have no labels for screen readers; that's fixed in the "
     "next build. The help desk reports fewer password-reset calls since the new login page "
     "went live, down from about forty a day to fewer than fifteen. And the training "
     "sessions for branch staff are booked: Leeds on the third, Bristol on the fifth, and "
     "Glasgow on the tenth of November. Please send questions before Friday, and thanks, "
     "everyone, for a productive week."),
    ("180-guide", "How to brew coffee", 180,
     "Here is how I make a good cup of filter coffee at home, step by step. Start with "
     "fresh beans. Coffee loses most of its aroma within a few weeks of roasting, so buy "
     "small bags and check the roast date printed on the side. Store them in an airtight "
     "container, away from sunlight, and never in the fridge, where they absorb smells and "
     "moisture. Next, the grind. For a paper filter, you want something like coarse sand. "
     "Too fine, and the water passes slowly, pulling out bitter compounds. Too coarse, and "
     "it rushes through, leaving the cup thin and sour. A burr grinder gives a much more even "
     "result than a blade grinder, and it's the single best upgrade for most people. Now, "
     "the ratio. I use sixty grams of coffee per litre of water, so for a single large mug "
     "of about three hundred millilitres, that's eighteen grams. Weigh it, if you can; "
     "spoons are surprisingly inconsistent. Heat the water to just off the boil, around "
     "ninety-four degrees Celsius. If you don't have a thermometer, let the kettle rest for "
     "thirty seconds after it clicks off. Rinse the paper filter with hot water first. This "
     "washes out the papery taste and warms the cone and the cup. Add the ground coffee, "
     "shake it level, and pour just enough water to wet all of it, about twice the weight of "
     "the coffee. Wait thirty to forty-five seconds. You'll see it bubble and swell; that's "
     "carbon dioxide escaping, and letting it out first gives a more even extraction. Then "
     "pour the rest slowly, in small circles, keeping the level steady rather than filling "
     "the cone to the top. The whole brew should take about three to four minutes. If it's "
     "much faster, grind finer next time; if it's much slower, grind coarser. Change one "
     "thing at a time, and taste carefully. Is it sharp and sour? Try a finer grind or "
     "hotter water. Is it harsh and bitter? Go coarser or shorten the brew. Finally, drink "
     "it soon. Coffee left on a hot plate turns stale and bitter within twenty minutes, so "
     "if you need more later, it's better to brew a second, smaller batch. A word on water, "
     "too, because it makes up almost the entire cup. Very hard water, the kind that leaves "
     "white scale in the kettle, mutes the flavour and makes it chalky. Very soft or "
     "distilled water extracts poorly and tastes flat. A simple jug filter is usually "
     "enough to land somewhere sensible in between. And clean your equipment: old oils "
     "build up in grinders and cones and turn rancid, so a quick wash every week, and a "
     "proper scrub every month, keeps everything tasting fresh. That's it: fresh beans, an "
     "even grind, a sensible ratio, good water, and a little patience."),
]


def by_id(pid):
    return next((p for p in PASSAGES if p[0] == pid), PASSAGES[0])


def label(passage):
    seconds = passage[2]
    length = f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"
    return f"{length} · {passage[1]}"
