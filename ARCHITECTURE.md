#Harness:

I decided to create a custom harness, since this is the first agent I have ever built so I wanted to learn more about it. This is controlled by mentor_harness.py. The basic conversation flow is:

greet (a special prompt run initially) -> loop: user input -> agent loop (LLM response -> tool calls -> transcript + tool calls and results passed to LLM ->repeat) -> output -> repeat

and if inside the agent loop the end_session tool is used, we get out to a wrap_up state which gives a wrap up message. 


#Memory:

### User profile fields
| Field | Type | Notes |
|---|---|---|
| demographic_info | dict | name, age, pronouns, etc. — only volunteered info |
| general_coder_level | enum | "unknown" or "beginner" or "intermediate" or "advanced" |
| language_skill_levels | dict | per-language enum values |
| user_preferences | string | ≤100 words |
| user_goals | string | ≤80 words |

stored in a single user_profile.json file.

### Agent self-assessment:
Agent self-assessment (private note from the agent to itself, from last session) stored in agent_self_assessment.txt

### Topic index: 
topic_index.json file including topics where each has id, title, and short_description. This is used for semantic search with search_topics tool.

Each topic with id topic_id then has a file topic_id.json in the memory/topics subfolder. It has the following fields:

### Topic detail fields
| Field | Mutable | Notes |
|---|---|---|
| id, title, short_description | No | Set at creation; never change |
| user_level | Yes | "unknown" or "beginner" or "intermediate" or "advanced" |
| note | Yes | ≤80 tokens; key observations on user understanding |

### Session index:
session_index.json file including a list (in chronological order) of session ids (which are timestamps; sessions are assumed to be non-overlapping, i.e. a single user can only have one instance of the agent open at a time) along with a session summary and topics_discussed (list) for each (both used for semantic search via search_sessions_by_summary or search_sessions_by_topic).

### Session transcripts:
For each session we have a session_id.json file in memory/sessions which contains the id, topics_discussed, summary, along with the full transcript. This can be retrieved via read_session tool if the model needs to look at a specific exchange in an old session. 


##Auto-loaded at session start:
- User profile
- Agent self-assessment
- Recent sessions (last 3): ids, summaries, topics

##On-demand (fetch only when needed):
- Topic detail via `read_topic_detail(topic_id)`. Use search_topics to find topic_ids of topics relevant to a natural language query if needed.
- Older sessions via `search_sessions_by_*` then `read_session`

##Memory lifecycle:
Long-term memory is read at the beginning of a session and written at the end of it, never in the middle. However, the intermediary is a "scratchpad" that the agent writes important observations/events/updates to during the session using a special tool (write_scratchpad). This should reduce the chances of the agent corrupting the long term memory due to short-sighted single-event memory updates (and generally give it fewer chances to hallucinate something in the memory). 

#Multi-turn context:
We keep the 15 most recent turns verbatim, and we keep a running summary of the session (both in the prompts to the LLM) that is updated every 15 turns (not overwritten but the LLM is prompted to summarize the most recent 15 turns *along* with the old summary). 



#Some thoughts:
I had planned a more elaborate memory system that involved a graph to store the topic relationships and help suggest new topics and verify prerequisites, but couldn't get it to work in time. I really struggled since I was mostly working with a local LLM (which kept refusing to follow the instructions) on my gaming GPU before running the last tests with Claude after loading some credits (after which I discovered the rate limits can be a problem so I added time delays in the llm.py file for the anthropic client). This was fun to think about and build, but I think my prompts could benefit from more reorganization and cleaning their logic. The architecture design and prompts are where I spent the vast majority of my time (since AI assistants make the coding itself pretty fast). 

