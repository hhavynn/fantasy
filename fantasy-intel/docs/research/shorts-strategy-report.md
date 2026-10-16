# Short-Form YouTube Strategy for the Fantasy Football Usage Project

## Executive assessment

The project is **well suited to a Shorts-first launch**, and with only about five hours per week and roughly twelve weeks of season runway, I would make Shorts the primary product rather than treating them as a secondary format cut out of an 8–12 minute weekly show. YouTube itself describes Shorts as a discovery mechanism capable of reaching new audiences across many niches, not just entertainment or trend-driven content, and says its Shorts and long-form recommendation systems are primarily optimized separately. Trending audio is also not required for Shorts to perform, which fits your no-music-or-YouTube-Audio-Library-only constraint. citeturn6view4

The strongest positioning is:

> **Fantasy football decisions from real NFL usage, not fantasy points or projections.**

That is much more differentiated than making generic waiver-wire, start/sit, or “five players you NEED” videos. Established fantasy publishers already show that there is demand for usage-oriented analysis: Underdog, for example, publishes a recurring usage model built around underlying opportunity such as targets, carries, air yards, and goal-line work. Your advantage would be narrower and more defensible: **one player, one auditable role signal, one implication, with every number traceable to the dataset.** citeturn11search1

The business case should **not** depend on Shorts ad revenue during this season. As of October 2026, the existing ad-revenue YPP path is 1,000 subscribers plus 10 million qualified Shorts views in 90 days. More importantly for your timeline, YouTube has already announced that beginning **February 1, 2027**, new creators applying for ads/Premium revenue sharing will need 20 million qualified Shorts views in 90 days or 8,000 qualified watch hours; Shorts ad/Premium revenue itself will require maintaining 10 million qualified Shorts views over the preceding 90 days. Your 2026 season therefore should be treated primarily as an **audience-validation, site-acquisition, and future sponsorship experiment**, not an ad-revenue experiment. citeturn1view0turn6view3

My high-level recommendation is:

**Ship one core Short format quickly, create three editorial variants around it, publish about three high-quality Shorts per week, and use the remaining season to learn which usage signals and hooks actually cause viewers to stop scrolling. Do not build an elaborate long-form system first.**

## What the channel should focus on

Using the Phase 0 inventory you supplied as the working source of truth, v1 has snap share, target share, air yards/air-yard share, aDOT, backfield and red-zone usage, team passing tendencies such as neutral pass rate/PROE, quarterback context, and schedule information. Routes and trustworthy first-read share are unavailable, so they should simply disappear from the editorial promise rather than being approximated.

That still leaves enough data for a compelling channel because the basic story you are telling is not “Who scored fantasy points?” It is:

> **Whose underlying role changed before everybody reacts to it?**

That is exactly the kind of idea Shorts handles well. YouTube's own Shorts product discussion emphasizes an immediate hook and a compact “mini-story” rather than compressing a normal long-form introduction into forty seconds. In YouTube's January 2025 discussion with Shorts product lead Todd Sherman and creator Jenny Hoyos, the advice was to capture attention essentially immediately, with Hoyos describing a first-second hook as critical. Another YouTube creator case study similarly emphasizes making the premise clear within the opening few seconds. citeturn3search2turn3search0

I would rank your content pillars like this:

| Priority | Series | Primary statistic | What the Short answers |
|---|---|---|---|
| Highest | **Usage Riser** | Target share | “Did this player's passing-game role materially expand?” |
| Highest | **Role Change** | Snap share | “Is this player actually earning more time on the field?” |
| High | **Opportunity Spike** | Air-yard share | “Is the offense giving this player substantially more downfield opportunity?” |
| High | **Backfield Takeover** | Backfield share | “Is one RB consolidating the usable role?” |
| Medium | **High-Leverage Role** | Red-zone share | “Who is consistently involved near the scoring area?” |
| Medium | **Offense Changed** | Neutral pass rate / PROE | “Did the environment around this player become more pass-friendly?” |
| Late-season high | **Fantasy Playoff Runway** | One usage stat + Weeks 15–17 schedule | “Does a strong existing role also have a favorable playoff path?” |

The most important choice is to **make one statistic the protagonist of each Short**. Supporting context can appear, but a viewer should be able to answer “What was that video about?” with one phrase: “Jaxon Smith-Njigba's target share,” “this RB's snap share,” “that receiver's air-yard share,” and so on.

This is also a useful defense against the channel becoming generic. Current fantasy channels commonly package videos around concepts such as breakouts and waiver targets; the Fantasy Footballers' current YouTube presence, for example, uses direct player-oriented framing such as “His breakout is here.” The opportunity for your channel is not to reject that kind of hook, but to make it **provable**: “Here is the one number behind the breakout case.” citeturn11search2

I would deliberately avoid Shorts centered on:

“Top five players this week,” generic rankings, start/sit lists based on expected fantasy output, “league winner” claims, reaction to touchdown totals, rest-of-season rankings without a role argument, and stat-dump videos containing six unrelated metrics. Those formats either conflict with your data contract or erase the project's biggest advantage.

## The default Short format

The ideal first format is approximately **32–42 seconds**, not because YouTube requires that duration—Shorts can currently be up to three minutes—but because your concept is a single statistical argument and does not need more room. YouTube supports vertical or square Shorts up to three minutes, so a native 9:16 export is appropriate. citeturn7search3turn7search10

At a working narration target of roughly 155–165 words per minute, this means about **85–110 spoken words**. That is enough for a hook, one number, context, interpretation, and a very short CTA.

The default storytelling structure should be:

| Time | Job | Example |
|---|---|---|
| 0–2 sec | **Hook** | “One number changed for `[PLAYER]` this week.” |
| 2–9 sec | **Reveal** | “His target share went from `[OLD]%` to `[NEW]%`.” |
| 9–20 sec | **Show the evidence** | Animate the weekly chart and label the change. |
| 20–31 sec | **Explain why it matters** | Connect the usage change to snaps, offensive environment, QB, or schedule. |
| 31–38 sec | **Verdict** | “The role is moving up / down / holding.” |
| 38–42 sec | **CTA** | “Full usage dashboard is linked on the channel.” |

There should be **no greeting, channel logo animation, “welcome back,” or explanation of what target share means before the hook**. The viewer should see the interesting player name and the surprising statistic essentially immediately. That follows YouTube's own creator guidance that the opening seconds of a Short have disproportionate importance and that successful Shorts storytelling is built around concise moments rather than a long setup. citeturn3search2turn3search0

A particularly strong opening frame would look conceptually like:

> **PLAYER NAME**  
> TARGET SHARE  
> **18% → 31%**

Then narration begins:

> “One number changed for `[PLAYER]` this week.”

The viewer can therefore understand the premise even with sound off. YouTube specifically recommends captions and text overlays when adapting material for Shorts because Shorts are often consumed without audio; it advises using text overlays to surface key information. citeturn7search2

The ending should be short enough that it does not feel like a commercial. You can also experiment with a linguistic loop:

> Opening: “One number changed for `[PLAYER]` this week…”  
> Ending: “…and that's why I'm watching one number.”

YouTube has highlighted looping endings as one technique Shorts creators use to create smoother rewatches, although I would use this sparingly rather than forcing every episode into the same trick. citeturn3search0

## Script library

The script generator should not be asked to “write a good fantasy football Short” from scratch. That creates exactly the hallucination and repetitive-content problems your claims architecture is designed to prevent.

Instead, player selection and all factual propositions should exist **before** the LLM sees the episode. The LLM's job is primarily rhetorical: turn approved claims into natural narration without introducing new facts.

### Usage riser

This should probably become the flagship format.

**Hook**

> “One number changed for `[PLAYER]` this week: target share.”

**Script**

> “One number changed for `[PLAYER]` this week: target share.  
>   
> He went from `[OLD_SHARE]%` in Week `[PREV_WEEK]` to `[NEW_SHARE]%` in Week `[WEEK]` — a `[DELTA]` percentage-point jump.  
>   
> And he played `[SNAP_SHARE]%` of the offensive snaps, so this wasn't just a handful of random targets.  
>   
> `[TEAM]` is at `[PASS_CONTEXT]%` in `[PASS_CONTEXT_NAME]`, which gives that receiving role room to matter.  
>   
> The takeaway isn't fantasy points. The role got bigger.  
>   
> Full usage data is linked on the channel.”

The primary claim is target share. Snap rate and offensive context are evidence supporting the interpretation rather than competing storylines.

### Role change

This catches changes before target volume necessarily follows.

**Hook**

> “Don't wait for targets to notice what happened to `[PLAYER]`.”

**Script**

> “Don't wait for targets to notice what happened to `[PLAYER]`.  
>   
> His snap share moved from `[OLD_SHARE]%` to `[NEW_SHARE]%` this week.  
>   
> That's a `[DELTA]` percentage-point change in how often the offense actually put him on the field.  
>   
> His target share was `[TARGET_SHARE]%`, so the receiving volume hasn't fully moved with the playing time yet.  
>   
> That's the signal I'm tracking next week: does the opportunity follow the role?  
>   
> Full usage chart is linked on the channel.”

This format is especially valuable because it does **not** pretend the data knows the future. It describes a measurable role change and explicitly identifies what needs confirmation next.

### Opportunity without results

This is the cleanest way to differentiate yourself from box-score content without actually using fantasy points.

**Hook**

> “This player's role looks different when you measure opportunity instead of results.”

**Script**

> “Look at `[PLAYER]`'s air-yard share.  
>   
> It reached `[AIR_YARD_SHARE]%` this week, with an average target depth of `[ADOT]` yards.  
>   
> His target share was `[TARGET_SHARE]%`, which means the offense wasn't only sending him deep on an occasional shot — he owned a meaningful portion of the passing opportunity.  
>   
> That doesn't guarantee production next week. It tells us what role the offense is giving him right now.  
>   
> That's the number I care about.  
>   
> Full usage dashboard is linked on the channel.”

The phrase “doesn't guarantee production” is useful. It protects the editorial identity: this is evidence about role, not a disguised projection model.

### Backfield takeover

**Hook**

> “This backfield changed more than the box score makes obvious.”

**Script**

> “`[PLAYER]` handled `[BACKFIELD_SHARE]%` of this backfield's opportunities in Week `[WEEK]`.  
>   
> Last week, that number was `[OLD_SHARE]%`.  
>   
> His snap share also moved to `[SNAP_SHARE]%`, so the workload change showed up in both touches and playing time.  
>   
> Against `[NEXT_OPPONENT]` next week, I don't need a projection to make the case.  
>   
> I just want to know whether this role holds.  
>   
> Full usage data is linked on the channel.”

Again, the important language is **“whether this role holds,”** not “start him,” “smash play,” or “he'll score.”

### Playoff runway

Do not make this a major series yet. Introduce it as the season gets closer to fantasy playoffs.

**Hook**

> “This role is already strong. The interesting part is Weeks 15 through 17.”

**Script**

> “`[PLAYER]` currently owns a `[PRIMARY_USAGE_STAT]%` `[STAT_NAME]`.  
>   
> I'm not changing the evaluation because of projected fantasy points. I'm looking at where that role will be used.  
>   
> In Weeks 15 through 17, `[TEAM]` gets `[OPPONENT_1]`, `[OPPONENT_2]`, and `[OPPONENT_3]`.  
>   
> Using our schedule metric, that stretch ranks `[RANK_OR_VALUE]`.  
>   
> Strong role first, schedule second. That's the playoff case.  
>   
> Full schedule dashboard is linked on the channel.”

That ordering—**role first, schedule second**—should become a permanent editorial rule.

### Titles

Titles should describe the tension rather than stuff keywords. YouTube's Shorts product discussion notes that thumbnails matter much less for discovery inside the Shorts feed than they do in traditional browse contexts, though they remain useful for channel presentation and branding. The same principle implies that the first visual and spoken hook deserve more development time than obsessively iterating thumbnail art. citeturn3search2

Good title families are:

| Style | Template |
|---|---|
| Direct | **`[PLAYER]`'s Role Just Changed** |
| Stat-led | **The `[STAT]` Change Behind `[PLAYER]`** |
| Curiosity | **One Number I'm Watching on `[PLAYER]`** |
| Contrarian | **Don't Evaluate `[PLAYER]` by the Box Score** |
| Trend | **`[PLAYER]` Is Gaining Real Usage** |
| Warning | **The Role Is Shrinking for `[PLAYER]`** |
| Playoffs | **`[PLAYER]`'s Weeks 15–17 Setup** |

Avoid titles such as “MUST START,” “LEAGUE WINNER,” “GUARANTEED BREAKOUT,” or “THIS PLAYER WILL EXPLODE.” They undermine the exact data-driven identity you are trying to build.

## Visual, voice, and originality design

The visuals should be extremely simple.

A Short does not need eight chart types. It needs **one visual question answered clearly**.

For target share:

```text
Week 4    ███████████ 18%
Week 5    ███████████████████ 31%
                         ↑ +13 pts
```

For snap share, animate the current bar growing from the prior week's value. For a multiweek trend, show only the last four or five observations rather than an entire season chart. For schedule content, show Weeks 15, 16, and 17 as three large cards rather than a dense league schedule table.

A useful shot rhythm would be:

**first frame stat → trend chart → one context card → takeaway card → CTA**

Do not move the camera simply because Shorts are supposed to be “fast.” Your content already changes visually as numbers animate. The charts themselves can provide motion.

Burned-in subtitles are the right choice. YouTube recommends text and captions for Shorts and also provides automatic captions, but warns that automatically generated captions can mis-transcribe speech and recommends reviewing them. Since your entire premise depends on exact numbers, letting automatic captioning turn “thirty-one percent” into the wrong number would be especially undesirable; your own deterministic captions are safer. citeturn7search2turn7search7

The no-music decision is also completely reasonable. YouTube explicitly says trending sounds are not necessary for Shorts success and that original ideas can work without them. For this product, the lack of music may actually reinforce the analytical, clean-data identity. citeturn6view4

The larger risk is not production quality. It is **looking mass-produced**.

YouTube's monetization policy explicitly says monetizable content should not be mass-produced, generic, or repetitive, and in July 2025 renamed its “repetitious content” policy to **“inauthentic content”** while clarifying that repetitive or mass-produced content can be ineligible for monetization. Reviewers can examine a channel's overall theme, newest videos, most-viewed content, metadata, titles, thumbnails, and other channel-level characteristics. citeturn6view2

That means your script-similarity QC should go beyond checking whether the words differ.

A channel with forty videos that all say:

> “Player X saw his target share increase from A to B. This is important because…”

can remain structurally mass-produced even if every player's numbers are correct.

Your originality gate should therefore evaluate four dimensions:

| Dimension | What should vary |
|---|---|
| Hook | Question, warning, surprise, comparison, “watch next week,” playoff angle |
| Analytical reason | Role expansion, role contraction, unresolved opportunity, offensive environment, schedule |
| Visualization | Change bars, trend line, before/after, schedule cards, role stack |
| Ending | Confidence level, next thing to monitor, ROS implication, playoff implication |

In other words: **template the production, not the analysis**.

Your use of nflverse data also remains commercially workable provided the applicable attribution and license obligations are followed. The nflverse organization states that most of its data is CC-BY 4.0 as of October 2026, while FTN-provided datasets are CC-BY-SA 4.0. Keeping the unverified FTN first-read material out of v1 avoids both the metric-quality problem you already identified and the additional share-alike considerations attached to FTN data. citeturn9search1turn9search2

For the AI voice, your planned “narrated with an AI voice” description disclosure is sensible. YouTube currently requires disclosure for realistic AI-generated or meaningfully altered content in specified circumstances, while script ideation and other production assistance generally do not by themselves require disclosure. YouTube also states that properly disclosing AI content does not itself reduce a video's audience or monetization eligibility. citeturn1view4

## Publishing and learning plan for the remaining season

Five hours per week across twelve weeks is roughly **60 total hours**. That is enough to validate a Shorts channel, but it is not enough to simultaneously perfect local LLM inference, TTS, rendering, long-form production, automated publishing, advanced thumbnails, sponsor infrastructure, site integration, and five different content formats.

The principle should be:

> **Automate only what saves time before the season ends.**

I would target approximately **three Shorts per week** after the first week. That produces roughly 30–35 published experiments before the regular season ends, which is enough to compare format families without creating an impossible editorial workload.

A practical weekly budget is:

| Work | Weekly budget |
|---|---:|
| Refresh data and generate candidates | 30–45 min |
| Review candidate players / choose three | 30 min |
| Review generated claims and scripts | 45–60 min |
| Render, QC, manual final review | 90–120 min |
| Upload / title / metadata | 30–45 min |
| Review prior analytics | 30–45 min |
| Buffer for failures | remaining time |

The first uploads should not wait for the perfect local pipeline. Your own priority says speed to first upload matters more than infrastructure perfection; I would therefore consider the pipeline successful as soon as it can reproducibly produce **one correct 9:16 Short with exact captions and an auditable claims file**.

For analytics, the most important YouTube-provided Shorts metrics include **Shown in Feed**, **How many chose to view**—the proportion that viewed instead of swiping away—views, likes, subscribers, and discovery sources. YouTube specifically recommends comparing successful and unsuccessful Shorts to find patterns rather than judging the channel from one viral outlier. citeturn3search5

Audience retention should be the second major diagnostic. YouTube creator guidance repeatedly emphasizes examining where viewers leave and revising composition based on those drop-offs; one official creator case study describes moving the core of the story earlier after retention data showed that excessive setup was hurting performance. citeturn3search4

Do **not** begin with internet folklore like “you need 80% viewed-vs-swiped” or “120% retention or the algorithm kills it.” YouTube does not provide a universal success threshold in the official material cited here. Establish your own baseline.

After the first ten Shorts, compare:

**Hook family:** number-first vs question-first vs contrarian.

**Primary metric:** target share vs snap share vs air-yard share.

**Duration:** approximately 30–34 seconds vs 38–42 seconds.

**First frame:** raw stat vs week-over-week delta.

**Ending:** CTA only vs “thing to watch next week” followed by CTA.

Change **one major variable at a time**. Otherwise you will have views but no idea why one Short outperformed another.

A useful editorial calendar could become:

| Slot | Format | Example purpose |
|---|---|---|
| Early week | **Usage Riser/Faller** | React to completed week's role changes |
| Midweek | **Hidden Opportunity** | Surface a player whose opportunity changed |
| Late week | **What I'm Watching Next** | Pair existing usage with offensive/schedule context |

Closer to the fantasy playoffs, the third slot becomes:

> **“Playoff Usage + Weeks 15–17”**

This keeps the channel timely without rebuilding the production system every week.

## Traffic, sponsorship, and monetization reality

There is one important flaw in a simple “every Short links to the site” strategy: **ordinary URLs in Shorts descriptions and Shorts comments are not clickable.** YouTube currently makes channel profile links clickable and also allows a Short to contain a clickable **Related Video** link, but URLs typed directly into a Short's description or comments remain non-clickable. citeturn6view0turn6view1

So after the website launches, do **not** end videos with:

> “Link in the description.”

That is the wrong CTA for Shorts.

Use:

> **“Full chart is in the first link on my channel.”**

Your channel can display up to fourteen profile links, so the fantasy site should be the first one. citeturn6view0

Even better, make the landing page correspond exactly to the promise in the video:

> “See every player's target-share trend.”

not:

> “Visit my fantasy football website.”

The former offers an immediate continuation of the Short; the latter sounds like an advertisement.

There is also a useful YouTube-native funnel once the site exists:

**Short → Related Video → site**

YouTube allows creators with advanced feature access to attach a clickable related-video link directly below the channel handle inside the Shorts player. citeturn6view1

That related video could eventually be a permanent two-minute horizontal “How the usage dashboard works” video. Because ordinary long-form video descriptions can contain clickable external URLs when the appropriate advanced features are enabled, it gives motivated viewers another route to the product. YouTube's link documentation explicitly distinguishes clickable long-form description links from non-clickable Shorts-description URLs. citeturn6view0

For the current period while the website is not ready, I would **not pretend there is a destination**. Use:

> “Follow for the next usage signal.”

or:

> “I'm building the full usage dashboard now—follow to see it when it launches.”

Then replace that line globally once the site is public.

Sponsors are a more plausible medium-term revenue path than expecting meaningful Shorts ads immediately. YouTube now has formal Creator Partnerships infrastructure that lets eligible creators maintain preferences for Shorts brand deals, and its brand-partner tools can share organic performance data such as retention, watch time, engagement, and viewer demographics with participating brands. YouTube also now supports clickable external brand-deal links on Shorts in supported partnership workflows. citeturn8search2turn8search5turn5search3

But build the audience before optimizing sponsor integrations.

The most useful eventual sponsor pitch will not be:

> “I have X subscribers.”

It will be:

> “Every week, this channel reaches fantasy players who actively watch data-driven role analysis, and here are the median views, retention, repeat-viewer behavior, audience demographics, and site visits.”

When sponsorships begin, YouTube requires creators to identify videos containing paid promotions using its paid-promotion controls. In the United States, the FTC additionally says a material connection to a brand should be clearly and conspicuously disclosed and that, for video endorsements, disclosure should appear **in the video itself**, not solely in the description. citeturn8search0turn12search0

The final strategic hierarchy should therefore be:

**this season:** audience and content-market fit →  
**when the site launches:** audience → site conversion →  
**after repeatable viewership:** sponsor experiments →  
**later:** YouTube ad revenue.

That ordering is especially important because YouTube's February 2027 changes make Shorts ad revenue a very high-volume milestone for a new creator, whereas your site's value and potential sponsor value can begin long before 20 million Shorts views. citeturn6view3

The most promising v1 is consequently not “an AI-generated fantasy football channel.” It is:

> **A fantasy-football usage channel with an unusually rigorous data pipeline, where automation stays invisible and every thirty-to-forty-second video makes one defensible argument.**

That distinction is important both editorially and commercially. YouTube explicitly wants original, authentic content rather than generic mass-produced output; your data provenance, numerical QC, human review, changing analytical angles, and narrow role-based editorial thesis can turn the automation from a monetization risk into the infrastructure that lets you publish consistently without sacrificing trust. citeturn6view2