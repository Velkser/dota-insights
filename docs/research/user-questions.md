# What Dota 2 players ask about stats

Tracker task p1-11. A catalog of the stat questions Dota 2 players ask in public, mapped to the
MCP tools we plan to build. Use it to check tool scope and to decide what to build first.

## Method and caveats

- **Paraphrased, not quoted.** Every question below is rewritten in neutral wording. Most come up
  many times across sources, so each one stands for a recurring kind of question, not a single post.
- **Sources are per group.** Each domain lists the public pages where its questions come up. A
  link shows where the topic is discussed. It doesn't mean that exact wording appears there.
- **Reddit.** reddit.com and old.reddit.com couldn't be fetched from the research environment, so
  most Reddit links are subreddit search URLs that reproduce the topic. Only one is a specific
  thread. Before quoting Reddit as evidence for a claim, re-check it in a browser.
- **YouTube/Twitch Q&A.** Recurring stream and video Q&A topics (for example "rate my build",
  "why can't I climb", "is hero X broken") are reflected in the questions, but no individual
  videos or VODs were captured as links in this pass. That's a known gap.
- **Russian-speaking communities.** dota2.ru, cyber.sports.ru, cybersport.ru and esports.ru.
  Questions from there are translated and marked `(RU)`.
- **No statistics are asserted here.** Where a source page quotes numbers, this document doesn't
  repeat them as fact. The answers are what the tools must compute.
- **Tool column.** It names the single planned tool that best answers the question. "no tool"
  means none of the planned tools covers it. Those questions are grouped into proposed tools at
  the end.

Question IDs are stable (`HER-01` and so on) so issues and tests can reference them.

---

## 1. Heroes

Sources:
[r/DotA2 search "hero winrate"](https://www.reddit.com/r/DotA2/search/?q=hero+winrate),
[r/learndota2 search "best hero to climb"](https://www.reddit.com/r/learndota2/search/?q=best+hero+to+climb),
[cyber.sports.ru: hero winrate vs experience on the hero (RU)](https://cyber.sports.ru/dota2/blogs/2908588.html),
[Statista: hero win rates](https://www.statista.com/statistics/1351237/dota2-heroes-win-rate/),
[dotacoach.gg meta by patch](https://dotacoach.gg/en/heroes/meta/7.38)

| ID | Question | Tool |
|----|----------|------|
| HER-01 | What is hero X's win rate, pick rate and ban rate right now? | get_hero_overview |
| HER-02 | Which heroes have the highest win rate in my bracket? | get_tier_list |
| HER-03 | Is hero X a good hero to climb with at my MMR? | get_hero_overview |
| HER-04 | Is hero X better in Herald–Crusader than in Immortal? | get_hero_overview |
| HER-05 | Does hero X get stronger or weaker the longer the game goes? | get_hero_power_curve |
| HER-06 | At what minute does hero X hit its peak? | get_hero_power_curve |
| HER-07 | What GPM, XPM and last hits should I hit on hero X by 10 minutes? | get_hero_benchmarks |
| HER-08 | Which position (1–5) does hero X win most in? | get_hero_overview |
| HER-09 | Is hero X better as a carry or as an offlaner? | compare_heroes |
| HER-10 | Which is stronger right now, hero X or hero Y, for the same role? | compare_heroes |
| HER-11 | Which underplayed heroes have a high win rate? | find_hidden_gems |
| HER-12 | How much does win rate on a hero go up after you've played it 50+ times? (RU) | get_hero_benchmarks |
| HER-13 | Which heroes are hard to learn but pay off later (low early win rate, high with experience)? | no tool |
| HER-14 | Which heroes are best for a total beginner? | get_tier_list |
| HER-15 | Which heroes have the highest average KDA or hero damage? | get_general_stats |
| HER-16 | Is hero X's win rate high only because few people play it? | get_hero_overview |
| HER-17 | Which heroes are strongest in turbo versus ranked? | get_tier_list |
| HER-18 | How has hero X's win rate moved over the last few patches? | get_hero_patch_history |

## 2. Matchups and counters

Sources:
[DOTAFire forum: guide to hero counters](https://dotafire.com/dota-2/forum/general/guide-to-hero-counters-4698),
[DOTAFire forum: picks and counterpicks question](https://www.dotafire.com/dota-2/forum/help/picks-counterpicks-question-51312),
[dota2.ru: counterpick table (RU)](https://dota2.ru/faq/221-tablica-kontrpikov-dota-2-kak-zakontrit-kazdogo-geroa-v-dote/),
[Dota2ProTips: counter methodology (Anti-Mage example)](https://www.dota2protips.com/heroes/antimage/counters),
[r/learndota2 search "counter"](https://www.reddit.com/r/learndota2/search/?q=counter)

| ID | Question | Tool |
|----|----------|------|
| MAT-01 | Which heroes counter hero X best? | get_hero_matchups |
| MAT-02 | Which heroes is hero X good against? | get_hero_matchups |
| MAT-03 | What is hero X's win rate against hero Y? | get_hero_matchups |
| MAT-04 | Is a "counter" from a counterpick site real or just two popular heroes overlapping? | get_hero_matchups |
| MAT-05 | Which heroes pair best with hero X on the same team? | find_hero_combos |
| MAT-06 | Which two-hero lane combos win the most? | find_hero_combos |
| MAT-07 | Which heroes counter an enemy lineup of illusion heroes? | suggest_pick |
| MAT-08 | Which items do players buy most when they face hero X, and do they help? | get_hero_builds |
| MAT-09 | Who should I pick against a Huskar mid? | suggest_pick |
| MAT-10 | Does hero X lose its lane to hero Y even if it wins the game later? | get_hero_lane_stats |
| MAT-11 | Which heroes are best against a team with lots of magic damage? | no tool |
| MAT-12 | Which three-hero cores win most together? | find_hero_combos |
| MAT-13 | Does a counter matter more in low or high MMR? | get_hero_matchups |
| MAT-14 | Which heroes have no hard counters at all? | get_hero_matchups |

## 3. Items

Sources:
[Dotabuff forum: the cost of Hand of Midas](https://es.dotabuff.com/topics/2014-01-07-the-cost-of-hand-of-midas),
[Dota2ProTracker: item lift](https://dota2protracker.com/items),
[Dota2ProTips: how the item data works](https://www.dota2protips.com/methodology/items),
[esports.gg: neutral items by tier](https://esports.gg/guides/dota-2/neutral-items-in-dota-2-all-tiers-and-inactive-neutrals/),
[r/DotA2 search "item winrate"](https://www.reddit.com/r/DotA2/search/?q=item+winrate)

| ID | Question | Tool |
|----|----------|------|
| ITM-01 | What is item X's win rate and who buys it? | get_item_overview |
| ITM-02 | Is item X's high win rate real, or do only winning players buy it? | get_item_overview |
| ITM-03 | How long does Hand of Midas take to pay for itself? | get_item_payback |
| ITM-04 | Is Battle Fury still worth it on hero X? | get_item_payback |
| ITM-05 | Battle Fury or Maelstrom first on hero X? | compare_items |
| ITM-06 | Black King Bar or Linken's Sphere against this lineup? | compare_items |
| ITM-07 | By what minute should item X be finished to still be good? | analyze_item |
| ITM-08 | How much does win rate drop for each minute item X is late? | analyze_item |
| ITM-09 | Which neutral item is best for hero X at each tier? | get_neutral_items |
| ITM-10 | Which neutral items get picked most in each tier? | get_neutral_items |
| ITM-11 | Which items have the highest win rate in this patch? | get_item_overview |
| ITM-12 | Which items are bought a lot but rarely help? | get_item_overview |
| ITM-13 | Is Dagon actually bad, or is it bought only when far ahead? | analyze_item |
| ITM-14 | Which support items correlate with winning (Glimmer, Force Staff, Pipe)? | get_item_overview |
| ITM-15 | Does buying a Divine Rapier win games or throw them? | analyze_item |
| ITM-16 | How many Observer Wards does a winning support buy? | analyze_vision |
| ITM-17 | Does Aghanim's Shard on hero X change its win rate? | analyze_item |
| ITM-18 | What does item X build from and how much does it cost? | lookup_reference |

## 4. Builds and abilities

Sources:
[DOTAFire forum: Anti-Mage, cleave or no cleave](https://www.dotafire.com/dota-2/forum/general/antimage-cleave-or-no-cleave-5900),
[DOTAFire forum: how to evaluate a build (Pugna example)](https://www.dotafire.com/dota-2/forum/general/how-to-evaluate-a-characters-build-items-pugna-example-3414),
[STRATZ blog: time to build](https://medium.com/stratz/time-to-build-hero-ccdf9a7255b5),
[r/learndota2: why Blink on Timbersaw](https://www.reddit.com/r/learndota2/comments/1kftjk5/why_blink_on_timbersaw/)

| ID | Question | Tool |
|----|----------|------|
| BLD-01 | What is the most common item build for hero X? | get_hero_builds |
| BLD-02 | Which build on hero X wins most, not just which is most popular? | get_hero_builds |
| BLD-03 | What skill order do top players use on hero X? | get_hero_builds |
| BLD-04 | Which talent at level 10/15/20/25 wins more on hero X? | get_ability_stats |
| BLD-05 | Which facet should I pick on hero X? | get_ability_stats |
| BLD-06 | Should I max Q or W first on hero X? | get_ability_stats |
| BLD-07 | What starting items do good players buy on hero X? | get_hero_builds |
| BLD-08 | Is Blink Dagger a core item on hero X now? | get_hero_builds |
| BLD-09 | What do pros build on hero X compared with pubs? | get_pro_builds |
| BLD-10 | Does an unusual build (for example, Radiance on hero X) actually work? | test_hypothesis |
| BLD-11 | Which ability of hero X gets the most kills or deals the most damage? | get_ability_stats |
| BLD-12 | How often do players skip their ultimate at level 6 on hero X? | get_ability_stats |
| BLD-13 | What does ability X do exactly, and what are its numbers? | lookup_reference |
| BLD-14 | What do top-1000 players build on hero X this week? (RU) | get_pro_builds |

## 5. Economy and farm

Sources:
[Steam discussion: what is good GPM for a carry](https://steamcommunity.com/app/570/discussions/0/371918937254317961),
[Dotabuff forum: how to increase GPM](https://www.dotabuff.com/topics/2014-05-19-how-to-increase-gpm-on-heroes),
[DOTAFire forum: tips to improve GPM](https://www.dotafire.com/dota-2/forum/help/tips-to-improve-gpm-38980),
[DOTAFire forum: 30-minute Battle Fury](https://dotafire.com/dota-2/forum/theorycrafting/agility-carries-30-minute-battlefury-3170)

| ID | Question | Tool |
|----|----------|------|
| ECO-01 | What is a good GPM for a carry? | get_hero_benchmarks |
| ECO-02 | What is a good GPM for my hero at my MMR? | get_hero_benchmarks |
| ECO-03 | Which heroes farm fastest? | get_farming_stats |
| ECO-04 | How much of hero X's gold comes from creeps, kills and neutrals? | get_farming_stats |
| ECO-05 | How many last hits should I have at 10 minutes? | get_hero_benchmarks |
| ECO-06 | How should gold be split across positions 1–5 in a winning game? | get_farm_distribution |
| ECO-07 | Is it bad if my support has more net worth than me? | get_farm_distribution |
| ECO-08 | How big a gold lead at 20 minutes usually wins the game? | get_gold_xp_curves |
| ECO-09 | Does an XP lead matter more than a gold lead? | get_gold_xp_curves |
| ECO-10 | How often do teams come back from a 10k gold deficit? | get_comeback_stats |
| ECO-11 | Does stacking camps make a measurable difference? | no tool |
| ECO-12 | How much gold do I lose when I die at minute 30? | lookup_reference |
| ECO-13 | Which heroes need the most farm to be useful? | get_farming_stats |
| ECO-14 | Does my GPM matter, or only my team's net-worth lead? | analyze_correlation |

## 6. Laning

Sources:
[r/learndota2 search "laning stage"](https://www.reddit.com/r/learndota2/search/?q=laning+stage),
[DOTAFire forum: early-game last hitting](https://www.dotafire.com/dota-2/forum/theorycrafting/early-game-last-hitting-2949),
[DOTAFire forum: Templar Assassin timing window](https://www.dotafire.com/dota-2/forum/general/templar-assassins-timing-window-and-ancient-stacking-48788)

| ID | Question | Tool |
|----|----------|------|
| LAN-01 | How often does hero X win its lane? | get_hero_lane_stats |
| LAN-02 | Which offlaners beat hero X in lane? | get_hero_lane_stats |
| LAN-03 | Does winning the lane actually win the game? | analyze_correlation |
| LAN-04 | Did I win or lose my lane in this match? | analyze_laning_phase |
| LAN-05 | What was my last-hit and deny count compared with the enemy laner? | analyze_laning_phase |
| LAN-06 | Is hero X better in a duo lane or solo? | get_hero_lane_stats |
| LAN-07 | Which safelane duos win their lane most? | find_hero_combos |
| LAN-08 | How many deaths in lane is "losing the lane"? | get_hero_benchmarks |
| LAN-09 | Does my mid player losing lane predict the game more than other lanes? | test_hypothesis |
| LAN-10 | When do supports usually leave lane to rotate in good games? | no tool |
| LAN-11 | How many lane creeps do I usually miss at my MMR? | get_player_stats |
| LAN-12 | Is a trilane ever worth it in pubs? | test_hypothesis |

## 7. Draft

Sources:
[Pulse: figuring out draft timings](https://www.pulse.bot/gaming/blogs/figuring-out-draft-timings-eb66cdca-280e-4c5c-8a90-572bad35d10e/),
[PC Gamer: Dota 2 data yields ideal team composition](https://www.pcgamer.com/dota-2-data-yields-ideal-team-composition),
[esports.gg: hero counters guide](https://esports.gg/guides/dota-2/dota-2-hero-counters/),
[r/learndota2 search "draft"](https://www.reddit.com/r/learndota2/search/?q=draft)

| ID | Question | Tool |
|----|----------|------|
| DRF-01 | Which draft has the better chance to win, ours or theirs? | analyze_draft |
| DRF-02 | What should I pick last, given these nine heroes? | suggest_pick |
| DRF-03 | Is our draft too weak in the late game? | analyze_draft |
| DRF-04 | What should I first-pick so I can't be countered? | suggest_pick |
| DRF-05 | Which heroes should we ban against this enemy? | no tool |
| DRF-06 | Do drafts with two hard carries win less? | test_hypothesis |
| DRF-07 | Does a team without stuns lose more? | test_hypothesis |
| DRF-08 | How much does the draft decide the outcome compared with play? | analyze_correlation |
| DRF-09 | Which hero combos are overrepresented in winning drafts? | find_hero_combos |
| DRF-10 | Is a five-melee lineup a bad idea? | test_hypothesis |
| DRF-11 | Who should I pick to fill our missing role? | suggest_pick |
| DRF-12 | Does first-picking hero X lose more than last-picking it? | test_hypothesis |
| DRF-13 | Which draft styles do pros favor (fight, push, late game)? | get_tournament_stats |
| DRF-14 | Can I see a win chance while drafting, like Dota Plus does? | analyze_draft |

## 8. Meta and patches

Sources:
[esports.ru: hero winrate changes after patch 7.37d (RU)](https://esports.ru/dota-2/news/izmenenie-vinrejta-geroev-v-dota-2-posle-patcha-7-37d/),
[cybersport.ru: three heroes gained winrate after 7.28 (RU)](https://www.cybersport.ru/dota-2/news/vinreit-trekh-geroev-v-dota-2-vyros-bolee-chem-na-5-posle-vykhoda-patcha-7-28),
[GosuGamers: understanding patch 7.11](https://www.gosugamers.net/dota2/news/46622-understanding-patch-7-11-welcome-changes-in-disguise),
[r/DotA2 search "patch winrate"](https://www.reddit.com/r/DotA2/search/?q=patch+winrate)

| ID | Question | Tool |
|----|----------|------|
| MET-01 | Which heroes got better or worse after the last patch? | find_meta_trends |
| MET-02 | What changed in the latest patch, in short? | get_patch_summary |
| MET-03 | What is the current tier list by role? | get_tier_list |
| MET-04 | Which heroes are rising in pick rate this week? | find_meta_trends |
| MET-05 | Did the nerf to hero X actually lower its win rate? | get_hero_patch_history |
| MET-06 | Is the meta more early-game or late-game than last patch? | find_meta_trends |
| MET-07 | Which items became popular after the patch? | find_meta_trends |
| MET-08 | Are average game durations getting longer or shorter? | get_general_stats |
| MET-09 | Is the pro meta different from the pub meta right now? | find_meta_trends |
| MET-10 | Which heroes are flavor of the month and likely to be nerfed? | find_meta_trends |
| MET-11 | How long does it take for a patch's meta to settle? | no tool |
| MET-12 | Which heroes have been strong for many patches in a row? | get_hero_patch_history |
| MET-13 | What is meta in my bracket rather than in high MMR? (RU) | get_tier_list |
| MET-14 | Which changes in the patch notes affect hero X? | get_patch_summary |

## 9. Single-match analysis

Sources:
[Steam discussion: stuck at MMR, analyze my game](https://steamcommunity.com/app/570/discussions/0/3570700856121696869),
[Team Liquid blog: Dota 2, the winner's advantage](https://www.teamliquid.net/blogs/482497-dota-2-the-winners-advantage),
[esports.ru: OpenDota overview (RU)](https://esports.ru/dota-2/wiki/opendota-polnyj-obzor-sajta-so-statistikoj-dota-2/),
[r/learndota2 search "replay analysis"](https://www.reddit.com/r/learndota2/search/?q=replay+analysis)

| ID | Question | Tool |
|----|----------|------|
| SMA-01 | Why did we lose this match? | analyze_match |
| SMA-02 | At what moment did the game turn? | find_turning_points |
| SMA-03 | What happened minute by minute in this game? | get_match_timeline |
| SMA-04 | Why did I die so much this game? | analyze_death |
| SMA-05 | Was this death my mistake or bad luck? | analyze_death |
| SMA-06 | Which teamfight decided the game? | analyze_teamfights |
| SMA-07 | Who did the most damage in fights, and who was out of position? | analyze_teamfights |
| SMA-08 | Did we have enough wards, and where were they? | analyze_vision |
| SMA-09 | Were my item timings good for this game? | analyze_match |
| SMA-10 | How did the gold and XP lead change over the game? | get_gold_xp_curves |
| SMA-11 | Did taking Roshan or towers actually help us here? | get_objective_impact |
| SMA-12 | Was my laning phase good in this match? | analyze_laning_phase |
| SMA-13 | Who was the weakest player in this game, by the numbers? | analyze_match |
| SMA-14 | Did we throw, or was it a fair loss? | find_turning_points |
| SMA-15 | Was our carry farming enough at 20 minutes? | get_farm_distribution |
| SMA-16 | How did this game compare with a typical game on my hero? | get_hero_benchmarks |
| SMA-17 | Which buyback decisions changed the game? | no tool |
| SMA-18 | Was there a smurf in this game? | no tool |

## 10. Personal player stats

Sources:
[Steam discussion: is the 50% win rate forced](https://steamcommunity.com/app/570/discussions/0/4039231510649573383),
[DOTAFire forum: is ranked MMR broken](https://www.dotafire.com/dota-2/forum/theorycrafting/is-ranked-mmr-broken-4765),
[GosuGamers: Dota and statistics, finding a balance](https://www.gosugamers.net/dota2/features/37440-dota-and-statistics-finding-a-perfect-balance),
[dota2.ru forum (RU)](https://dota2.ru/forum/),
[r/learndota2 search "my stats"](https://www.reddit.com/r/learndota2/search/?q=my+stats)

| ID | Question | Tool |
|----|----------|------|
| PER-01 | What are my overall stats (win rate, KDA, GPM)? | get_player_profile |
| PER-02 | Which heroes do I win most on? | get_player_hero_pool |
| PER-03 | Is my hero pool too wide or too narrow? | get_player_hero_pool |
| PER-04 | What is my biggest weakness, by the numbers? | find_player_weaknesses |
| PER-05 | Why am I stuck at my MMR? | find_player_weaknesses |
| PER-06 | Am I improving over the last 100 games? | get_player_trends |
| PER-07 | Do I play worse late at night or after a loss streak? | get_player_trends |
| PER-08 | Which friends do I win most with? | get_player_peers |
| PER-09 | How do my stats compare with my friend's? | compare_players |
| PER-10 | How do my stats compare with the average at my rank? | get_player_stats |
| PER-11 | Am I better as a core or as a support? | get_player_stats |
| PER-12 | What is my win rate in solo queue versus party? | get_player_stats |
| PER-13 | Do I win more on Radiant or Dire? | get_player_stats |
| PER-14 | Which of my heroes has the best GPM compared with other players on it? | get_player_hero_pool |
| PER-15 | Which enemy heroes do I lose against most? | get_player_stats |
| PER-16 | Find my games where I played hero X and lost with a lead. | find_matches |
| PER-17 | What is my ranked percentile? | get_rank_distribution |
| PER-18 | Do I die more often than other players on my hero? | find_player_weaknesses |
| PER-19 | How much MMR will I gain or lose next game? | no tool |
| PER-20 | Do longer games hurt my win rate? | get_player_stats |

## 11. Pro scene

Sources:
[GosuGamers: TI 2024 meta report, why Radiant dominated](https://gosugamers.net/dota2/features/73011-ti-2024-group-stage-and-seeding-decider-meta-report-why-is-radiant-so-dominant),
[win.gg: why the Radiant advantage disappeared in pro games](https://win.gg/news/why-has-the-radiant-advantage-disappeared-in-pro-dota-2-games/),
[esports.gg: DPC longest games, biggest comebacks](https://esports.gg/news/dota-2/tour-1-dpc-division-i-longest-games-biggest-comebacks-highest-kdas/),
[Dota2ProTracker: tournaments](https://dota2protracker.com/tournaments/18111)

| ID | Question | Tool |
|----|----------|------|
| PRO-01 | What were the results of yesterday's pro matches? | get_pro_matches |
| PRO-02 | How is team X doing this season? | get_team_stats |
| PRO-03 | What is the head-to-head record between team X and team Y? | get_head_to_head |
| PRO-04 | Which heroes were picked and banned most at this tournament? | get_tournament_stats |
| PRO-05 | Which hero had the highest win rate at TI? | get_tournament_stats |
| PRO-06 | What heroes does pro player X play most? | get_pro_player |
| PRO-07 | What does pro player X build on hero Y? | get_pro_builds |
| PRO-08 | Which teams like to play a fast, aggressive style? | get_team_stats |
| PRO-09 | Which pro player has the best KDA this year? | get_pro_player |
| PRO-10 | Who will win team X vs team Y? | no tool |
| PRO-11 | Does Radiant or Dire win more in pro games this patch? | get_tournament_stats |
| PRO-12 | Which heroes does team X always ban? | get_team_stats |
| PRO-13 | How long do pro games last compared with pubs? | get_general_stats |
| PRO-14 | Which pro games had the biggest comeback this year? | get_records |

## 12. Live

Sources:
[University of York: win prediction in live professional matches](https://eprints.whiterose.ac.uk/152931/),
[DEV Community: real-time draft prediction system](https://dev.to/t9mple_0d32462141e31abf09/building-a-real-time-dota-2-draft-prediction-system-with-machine-learning-4n39),
[r/DotA2 search "win probability"](https://www.reddit.com/r/DotA2/search/?q=win+probability)

| ID | Question | Tool |
|----|----------|------|
| LIV-01 | Which pro matches are live right now? | get_live_matches |
| LIV-02 | Who is winning this live game and by how much? | get_live_matches |
| LIV-03 | What is the win chance for each team in this live game? | predict_live_win |
| LIV-04 | Can this team still come back from where they are now? | predict_live_win |
| LIV-05 | Which high-MMR pub games are live with hero X? | get_live_matches |
| LIV-06 | Is my friend in a game now, and how is it going? | get_live_matches |
| LIV-07 | How accurate is the Dota Plus win-probability graph? | no tool |
| LIV-08 | Has a team ever won from this net-worth deficit at this minute? | get_comeback_stats |

## 13. General stats and records

Sources:
[win.gg: new highest GPM record (Medusa)](https://win.gg/news/a-new-highest-dota-2-gpm-record-set-by-ark-on-medusa/),
[esports.net: longest Dota 2 games in history](https://www.esports.net/news/dota/longest-dota-2-games-in-history/),
[ONE Esports: TI records](https://www.oneesports.gg/dota2/the-international-records-ti12),
[dota2protracker: Radiant vs Dire](https://dota2protracker.com/radiant-dire)

| ID | Question | Tool |
|----|----------|------|
| GEN-01 | What is the longest Dota 2 game ever played? | get_records |
| GEN-02 | What is the highest GPM ever recorded? | get_records |
| GEN-03 | What is the most kills in one game? | get_records |
| GEN-04 | How long does an average game last? | get_general_stats |
| GEN-05 | How many ranked games are played per day? | get_general_stats |
| GEN-06 | How are players spread across ranks (what share is Immortal)? | get_rank_distribution |
| GEN-07 | What rank is the average player? | get_rank_distribution |
| GEN-08 | What is the Radiant win rate overall? | get_general_stats |
| GEN-09 | Which hero has been picked most in history? | get_general_stats |
| GEN-10 | Which is the fastest-ending game on record? | get_records |
| GEN-11 | What is the most last hits in one game? | get_records |
| GEN-12 | Which region has the longest games? | get_general_stats |
| GEN-13 | How many games in my bracket end in an abandon? | no tool |
| GEN-14 | Show me all games where hero X had over 30 kills. | explore_query |

## 14. Myths and things players want tested

Sources:
[win.gg: Valve dev debunks the forced 50% win-rate theory](https://win.gg/news/valve-dev-debunks-dota-2-players-forced-50-win-rate-theory/),
[Hawk Live: how first blood decided the winner at TI](https://hawk.live/posts/the-international-dota-2-how-first-blood-decided-the-winner),
[win.gg: Radiant side advantage in the current meta](https://win.gg/news/radiant-side-has-a-massive-advantage-in-the-current-dota-2-meta/),
[Steam discussion: forced 50% win rate](https://steamcommunity.com/app/570/discussions/0/4039231510649573383),
[r/DotA2 search "myth"](https://www.reddit.com/r/DotA2/search/?q=myth)

| ID | Question | Tool |
|----|----------|------|
| MYT-01 | Does the team that gets first blood win more often? | test_hypothesis |
| MYT-02 | Does Radiant really have an advantage? | test_hypothesis |
| MYT-03 | Does matchmaking force everyone to a 50% win rate? | no tool |
| MYT-04 | Does the team that kills Roshan first win more? | get_objective_impact |
| MYT-05 | Is there "loss streak" matchmaking that gives you bad teammates? | no tool |
| MYT-06 | Do party players beat solo players? | test_hypothesis |
| MYT-07 | Does picking a support last mean you lose? | test_hypothesis |
| MYT-08 | Do games with an early Midas lose more often? | test_hypothesis |
| MYT-09 | Does losing the first tower decide the game? | get_objective_impact |
| MYT-10 | Do teams with more wards win more, or do winners just buy more wards? | analyze_correlation |
| MYT-11 | Is a high item win rate just survivor bias? | analyze_correlation |
| MYT-12 | Does a hero's win rate drop when it gets popular? | analyze_correlation |
| MYT-13 | Are games after 50 minutes a coin flip? | get_comeback_stats |
| MYT-14 | Is there a hidden MMR separate from the visible rank? | no tool |

---

## Question counts per tool

206 questions in total: 191 mapped to a planned tool and 15 with "no tool". Every planned tool
answers at least one question. Tools with a count of 1 may be worth merging into a neighbor.

| Tool | Questions |
|------|-----------|
| test_hypothesis | 12 |
| get_general_stats | 8 |
| get_hero_benchmarks | 7 |
| get_player_stats | 7 |
| analyze_correlation | 6 |
| find_meta_trends | 6 |
| get_hero_builds | 6 |
| get_hero_matchups | 6 |
| get_records | 6 |
| analyze_item | 5 |
| find_hero_combos | 5 |
| get_ability_stats | 5 |
| get_hero_overview | 5 |
| get_item_overview | 5 |
| get_tier_list | 5 |
| suggest_pick | 5 |
| get_hero_lane_stats | 4 |
| get_live_matches | 4 |
| get_tournament_stats | 4 |
| analyze_draft | 3 |
| analyze_laning_phase | 3 |
| analyze_match | 3 |
| find_player_weaknesses | 3 |
| get_comeback_stats | 3 |
| get_farm_distribution | 3 |
| get_farming_stats | 3 |
| get_gold_xp_curves | 3 |
| get_hero_patch_history | 3 |
| get_objective_impact | 3 |
| get_player_hero_pool | 3 |
| get_pro_builds | 3 |
| get_rank_distribution | 3 |
| get_team_stats | 3 |
| lookup_reference | 3 |
| analyze_death | 2 |
| analyze_teamfights | 2 |
| analyze_vision | 2 |
| compare_heroes | 2 |
| compare_items | 2 |
| find_turning_points | 2 |
| get_hero_power_curve | 2 |
| get_item_payback | 2 |
| get_neutral_items | 2 |
| get_patch_summary | 2 |
| get_player_trends | 2 |
| get_pro_player | 2 |
| predict_live_win | 2 |
| compare_players | 1 |
| explore_query | 1 |
| find_hidden_gems | 1 |
| find_matches | 1 |
| get_head_to_head | 1 |
| get_match_timeline | 1 |
| get_player_peers | 1 |
| get_player_profile | 1 |
| get_pro_matches | 1 |
| no tool | 15 |

## "No tool" questions, grouped into proposed new tools

| Proposed tool | Questions | What it would do | Data feasibility |
|---------------|-----------|------------------|------------------|
| `explain_matchmaking` | MYT-03, MYT-05, MYT-14, PER-19 | Explain how MMR, rank and matchmaking work, citing Valve's public statements, and show the player's own observed MMR swings. | Valve doesn't publish the algorithm. A reference answer plus the player's own history is possible. Predicting exact MMR change isn't. |
| `get_hero_learning_curve` | HER-13 | Win rate on a hero by how many games the player has on it. | Needs per-player hero game counts joined to matches. Could later merge into get_hero_benchmarks. |
| `get_damage_profile_counters` | MAT-11 | Rank heroes and items against a lineup's damage type (magic, physical, pure). | Needs hero damage breakdown by type from parsed replays. |
| `suggest_bans` | DRF-05 | Suggest bans given the enemy's or a player's hero pool and our draft. | Same inputs as suggest_pick. Probably a mode of suggest_pick, not a new tool. |
| `get_jungle_stats` | ECO-11 | Stacking, camp clears and their link to farm and win rate. | Needs parsed replays (camp stacks are in parsed match data). |
| `get_rotation_stats` | LAN-10 | When supports leave lane and how that relates to the result. | Needs positional data from replays. Expensive. |
| `analyze_buybacks` | SMA-17 | Buyback timing and its effect on fights and the result. | Buyback events are in parsed match logs. Could live inside analyze_match. |
| `detect_smurf` | SMA-18 | Flag accounts whose performance doesn't fit their rank and history. | Possible from account age, games and performance. Must be framed as a heuristic, never an accusation. |
| `evaluate_win_model` | LIV-07 | Check how well a win-probability model's percentages match real outcomes. | We can calibrate our own predict_live_win. Dota Plus percentages aren't public, so it can't audit Dota Plus itself. |
| `predict_pro_match` | PRO-10 | Pre-match win chance between two pro teams. | Possible from team stats and head-to-head. Close to betting content, so it needs a policy decision before building. |
| `get_patch_settle_time` | MET-11 | How many days after a patch hero win rates stop moving. | Computable from daily hero stats. Could be a mode of find_meta_trends. |
| `get_abandon_stats` | GEN-13 | Abandon and leaver rates by bracket. | Leaver status is in match data. Could be a field in get_general_stats. |

## Suggested priority order

Ordered by how many questions a tool answers, how often those questions come up, and whether
other tools depend on it.

1. **Hero core:** get_hero_overview, get_tier_list, get_hero_matchups, get_hero_benchmarks,
   get_hero_builds. Most common questions across every source, and the base data other tools use.
2. **Hypothesis engine:** test_hypothesis, analyze_correlation. They answer the whole myths group
   plus many draft and laning questions, and they stop us asserting causation from raw win rates.
3. **Single-match analysis:** analyze_match, analyze_death, find_turning_points,
   get_match_timeline, analyze_laning_phase. The most personal "why did I lose" questions. They
   need parsed replays, so they depend on the replay pipeline.
4. **Personal stats:** get_player_profile, get_player_stats, get_player_hero_pool,
   find_player_weaknesses, get_player_trends. The "why am I stuck" group.
5. **Items:** get_item_overview, analyze_item, compare_items, get_item_payback. Item questions
   need selection-bias handling (ITM-02, ITM-13), so build them after the hypothesis engine.
6. **Draft:** analyze_draft, suggest_pick, find_hero_combos (add a ban mode to suggest_pick).
7. **Meta and patches:** find_meta_trends, get_hero_patch_history, get_patch_summary.
8. **General and records:** get_general_stats, get_records, get_rank_distribution. Cheap to
   build from aggregates.
9. **Pro scene:** get_pro_matches, get_team_stats, get_tournament_stats, get_pro_player,
   get_pro_builds, get_head_to_head.
10. **Live:** get_live_matches, predict_live_win. Low question count and needs real-time data.
    Build last.
11. **New tools:** explain_matchmaking first (four questions, mostly reference text). Fold
    suggest_bans, get_patch_settle_time, get_abandon_stats and analyze_buybacks into existing
    tools as modes. Decide on predict_pro_match and detect_smurf as policy questions before
    building them.
