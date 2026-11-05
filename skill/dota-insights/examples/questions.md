# Example questions and the calls that answer them

**"Is Phantom Assassin good right now in Ancient?"**
`get_hero_overview(hero="Phantom Assassin", filters={"rank_bracket": "ancient"})`
→ win rate with interval, pick rate, positions, trend, sample size.

**"What counters Huskar?"**
`get_hero_matchups(hero="Huskar")` → heroes with the largest matchup advantage against him
(use the advantage, not the raw win rate).

**"We have Axe and Lion against Sniper and Drow; what should I pick mid?"**
`suggest_pick(allies=["Axe", "Lion"], enemies=["Sniper", "Drow Ranger"], filters={"position": 2})`

**"Is an early BKB worth it on Faceless Void?"**
`analyze_item(item="Black King Bar", hero="Faceless Void")` → win rate by purchase-minute
bucket. Mention the selection-bias caveat.

**"Analyse match 8012345678"**
`analyze_match(match_id=8012345678)`, then `find_turning_points(match_id=8012345678)` if the
user asks why the game was won or lost.

**"Where am I weaker than players at my rank? Account 56939869"**
`find_player_weaknesses(player="56939869")`

**"Which heroes rose the most after the last patch?"**
`find_meta_trends()` (defaults to the current patch against the previous one).

**"Сколько стоит смерть на 25-й минуте и когда выкупаться?"**
`analyze_death()`. Answer in Russian.

**"Пудж или Лион на четвёрку в Легенде?"**
`compare_heroes(heroes=["Пудж", "Лион"], filters={"position": 4, "rank_bracket": "legend"})`
