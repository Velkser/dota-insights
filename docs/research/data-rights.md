# Data rights: OpenDota, STRATZ, Steam Web API / Valve

Tracker task: p1-04. Researched 2026-11-04 from public pages. **This is not legal advice.**
It is an engineering summary to decide what needs a lawyer or a direct question to the data
provider. Quotes are kept short. Follow the links for full text.

Our plan: store and aggregate match data, cache API responses, publicly serve derived stats
through an MCP server and site, start with a free tier, then add a paid Pro tier and API.

## OpenDota

Sources:

- FAQ (serves as the privacy policy and terms; the site footer's "Privacy & Terms" link points
  here): <https://blog.opendota.com/2014/08/01/faq/>
- API pricing page: <https://www.opendota.com/api-keys>. The text comes from
  [odota/web `src/lang/en-US.json`](https://github.com/odota/web/blob/master/src/lang/en-US.json),
  the price from [`src/components/Api/Api.tsx`](https://github.com/odota/web/blob/master/src/components/Api/Api.tsx),
  and the limits from the live `https://api.opendota.com/api/metadata`.
- API spec intro: [odota/core `svc/api/spec.ts`](https://github.com/odota/core/blob/master/svc/api/spec.ts)
- Repo license: MIT ([odota/core](https://github.com/odota/core))

What the terms say:

- **No data license or API terms of service exist.** The FAQ offers no terms beyond
  "The service is provided on a best-effort basis", and the only license mentioned is for the
  code: "The code is under the MIT license." There is no clause about storing, caching or
  redistributing API data. **Status: uncertain**, meaning not prohibited, but not granted
  either.
- **Commercial use:** OpenDota sells API access ("Premium Tier", "$0.01 per 100 calls", key
  "requires payment method"). That implies commercial API use is expected. Nothing grants
  rights to *resell* the data, and nothing forbids it.
- **Attribution:** for the code, the authors "would also appreciate a credit/attribution".
  This is a request, not a requirement. No data attribution rule exists. We should credit
  OpenDota anyway.
- **Limits:** the free tier needs no key, allows 60 calls/min and 3,000 calls/day (live
  metadata `freeCallLimit: 3000`, `freeRateLimit: 60`). The premium tier is unlimited calls
  at 3,000 calls/min (`premRateLimit`), and "Responses with status codes 404, 429 or 500
  aren't billed."
- **Data origin:** "All of the data provided comes from public sources such as the Steam
  WebAPI", plus replay parsing. Whether Valve's API terms flow down to OpenDota's consumers
  is **uncertain** (see Valve below).
- **Player privacy:** OpenDota relies on Valve's "Expose Public Match Data" setting.
  Opting out "causes you to appear as anonymous in future matches" but "existing data is
  not deleted".

## STRATZ

Sources:

- API page (token tiers, conditions): <https://stratz.com/api> (rendered in Russian for us;
  quotes below are our translation unless marked EN)
- FAQ (EN): <https://stratz.com/knowledge-base/API>, especially
  ["Is the STRATZ API free?"](https://stratz.com/knowledge-base/API/Is%20the%20STRATZ%20API%20free%3F)
- **We found no general Terms of Service page.** `stratz.com/terms` returns "page not found",
  and the sitemap has no terms or privacy entry. The `/api` page itself carries the usage
  conditions and says they may change ("we may add and change the terms of use of our API
  over time").

What the terms say:

- **Free:** EN FAQ: "the STRATZ API is available 100% for free!"
- **Attribution (Default token):** EN FAQ: "all we ask is that you leave a link back to us,
  referencing where you got the data." The `/api` page calls a "Powered by STRATZ" notice
  appreciated rather than mandatory. Badges are offered for this.
- **Referral traffic (Individual / Multi tokens):** these tokens need an application and must
  generate referral traffic to STRATZ (1,000 and 5,000 clicks per month according to the
  table). STRATZ "will periodically check", and a token that falls short "may be
  deactivated". In practice this means a required, prominent link-back.
- **Approval is discretionary:** STRATZ may reject an application "if the intended use does
  not match the requested amount of data".
- **Prohibited:** illegal use. Also any app that "supports or facilitates cheating, scripting,
  hacking, violates the Steam User Agreement or harms the integrity of Dota 2 and/or esports".
  Live in-game assistance during a match could fall under this.
- **Storing, caching, aggregating, commercial use:** **not addressed anywhere we could find.**
  STRATZ runs a paid subscription (STRATZ+) itself, so a paid competitor built on its data is
  the most likely point of friction. **Status: uncertain. Ask STRATZ (Discord) before launching
  a paid tier that uses STRATZ-derived data.**
- Footer: "© 2015-2026 STRATZ, LLC. All rights reserved." STRATZ's own metrics (IMP, win
  predictions, lane outcome classes) are STRATZ's derived work. Serving them raw is riskier
  than serving our own aggregates computed from underlying match facts.

## Steam Web API / Valve

Sources:

- Steam Web API Terms of Use (last updated July 2010): <https://steamcommunity.com/dev/apiterms>
- Steam Subscriber Agreement: <https://store.steampowered.com/subscriber_agreement/>
  (referenced by the API terms; not reviewed in detail here)

What the terms say (section numbers from the API terms):

- **License scope (§2):** you may "distribute Steam Data to end users for their personal use
  via your Application". The "Application" is the site, app or service named when the key
  was registered.
- **Only fetch what users ask for (§2):** "You will only retrieve Steam Data about a Steam end
  user as requested by the end user." Read literally, this conflicts with bulk-crawling player
  profiles. Sequence-number match ingestion is standard practice (OpenDota, STRATZ and
  Dotabuff all do it), but **the clause is still uncertain for us**.
- **Storage (§2):** "You will inform the end user about any Steam Data you will store" and
  store it "in a country (or countries) identified in your privacy policy". You **must post a
  privacy policy** about nonpublic end-user data.
- **Disclaimers (§2):** Steam Data must be provided "as is", with disclaimers like §7–8.
- **Key (§2):** "personal to you and specific to your Application". Keep it confidential and
  don't share it.
- **No implied endorsement (§2, §5):** Steam Data may not be presented so it appears your app
  is "endorsed or affiliated with Valve or Steam". You may say you implemented the Steam Web
  API, nothing more.
- **Fair play (§2):** don't create technology that "may give a user an unfair competitive
  advantage when playing multiplayer versions of any Steam game". This matters for any live
  or in-match feature.
- **No unsolicited marketing (§2).** Limit: **100,000 calls/day** (§2).
- **Brand and links (§3):** a license to use Valve names, logos and links on pages with Steam
  Data. Links to Valve must not be `nofollow`.
- **Termination (§1, §4, §11):** Valve "may change or terminate the Steam Web API entirely or
  your access in particular".
- **Commercial use:** **not explicitly addressed.** Nothing prohibits charging for our own
  service. But "for their personal use" makes **reselling raw Steam Data through a paid
  developer API** (our planned API tier) the riskiest part. Serving our own aggregates is
  lower risk. **Status: uncertain.**
- **Game assets:** hero names, icons and item art are Valve IP. The API terms license only
  "Valve name(s), logo(s), and links". We found no Valve fan-site or asset-use policy for
  Dota 2 in this pass. **Uncertain.** Common practice (OpenDota, STRATZ, Dotabuff) is to use
  them with a "Dota 2 is a registered trademark of Valve Corporation" notice. STRATZ's
  footer carries exactly that kind of notice.

## Summary

| Question | OpenDota | STRATZ | Steam Web API / Valve |
| --- | --- | --- | --- |
| Store raw responses | Not addressed (uncertain); no restriction found | Not addressed (uncertain) | Allowed with conditions: tell users what's stored, privacy policy, named storage country |
| Cache | Not addressed; normal practice | Not addressed; effectively required by the quota | Allowed under the storage conditions above |
| Aggregate / derive our own stats | Not addressed; low risk | Not addressed; low risk for our own aggregates, higher for re-serving STRATZ metrics (IMP, win predictions) | Allowed within "personal use" of our end users |
| Publicly serve derived stats | Not addressed; widely done | Allowed in spirit (link-back requested); not explicit | Allowed to end users; no implied Valve endorsement |
| Attribution | Requested for code, not required; give it anyway | **Required in practice**: link-back (Default) or referral traffic (Individual/Multi) | Valve trademark notice; no-endorsement wording; follow links |
| Commercial use / paid tier | Not prohibited; OpenDota sells API access itself | **Uncertain**; ask STRATZ first | Not prohibited for our own service; **reselling Steam Data via a paid API is uncertain** |
| Rate limits | Free 60/min, 3k/day; paid 3k/min, $0.01 per 100 calls | 20/s, 250/min, 2k/h, 10k/day (Default); 4k/h, 20k/day (Individual) | 100k/day per key |
| Can access be revoked | Best effort, no SLA | Yes (referral checks, misuse) | Yes, at Valve's discretion |

## Open risks

1. **STRATZ and a paid tier.** No terms cover commercial use. STRATZ has a competing paid
   product, and higher tokens are approved at STRATZ's discretion. *Mitigation:* ask STRATZ
   in writing (Discord or email) before Pro launch. Keep STRATZ optional and swappable, and
   don't put STRATZ-proprietary metrics behind the paywall.
2. **Valve "personal use" and "as requested by the end user".** Our planned developer API
   (Pro/API tier) redistributes data to third parties, not to end users for personal use.
   Bulk-collecting player data isn't "requested by the end user". *Mitigation:* serve only
   aggregates and match facts. Fetch Steam profile data only when a user looks up that
   profile. Get legal review before launching the paid API.
3. **Personal data (GDPR and similar).** Account IDs, persona names and match histories are
   personal data. Valve requires a privacy policy, and EU law adds rights such as erasure.
   OpenDota's "existing data is not deleted" approach may not be enough for us. *Mitigation:*
   privacy policy before launch, a delete/opt-out path, and respect for anonymous
   (`4294967295`) players.
4. **No written terms at OpenDota.** Their silence is not a license, and they could add terms
   later. *Mitigation:* attribute them, watch their FAQ and API page, and keep provenance on
   every stored record so we can purge one source's data if needed.
5. **Valve game assets** (hero and item icons, names) have no explicit license for our use.
   *Mitigation:* trademark notice, no Valve logos implying partnership, and no asset use
   beyond what's customary.
6. **Fair-play clauses (Valve §2, STRATZ "cheating, scripting").** Live or in-match advice
   features (live draft helper, live tool) could be read as an unfair competitive advantage.
   *Mitigation:* keep live features to spectating and analysis, and get a decision before
   building an in-game overlay.
7. **Terms drift.** The STRATZ page says its terms can change at any time. The Steam terms
   date from 2010 and could be rewritten. *Mitigation:* re-check these pages before each
   paid-tier milestone, and record the date checked in this file.
