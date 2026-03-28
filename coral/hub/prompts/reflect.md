## Heartbeat: Reflection

Pause and reflect on your recent work. Write a note in `{shared_dir}/notes/`.

### Anchor in concrete results
Review your recent attempts (`coral log -n 5 --recent`). What specific changes led to score improvements or regressions?

*Example: "Attempt abc123 improved score from 0.72 to 0.78 by adding batch normalization after each conv layer."*

### Examine surprises
What surprised you? What didn't go as expected? Surprises reveal gaps in your mental model.

*Example: "I expected dropout to help with overfitting, but validation loss actually increased. Maybe the model is underfitting, not overfitting."*

### Analyze causes
For your most significant result (good or bad): *why* did it happen? What's the underlying mechanism?

*Example: "The score dropped because the new loss function has different gradient dynamics — it saturates near 0, causing vanishing gradients in early layers."*

### Assess confidence
How certain are you about your current approach? What evidence would change your mind?

*Example: "70% confident that architecture changes will help more than hyperparameter tuning. Would reconsider if 3 more architecture changes show <1% improvement."*

### Plan next experiment
Based on this reflection, what's one specific thing to try next? What do you expect to happen?

*Example: "Try replacing ReLU with GELU in the attention layers. Expect ~1-2% improvement based on similar findings in the transformer literature."*

---
**Saving your note:** The `{shared_dir}/notes/` directory is organized like a file system. Browse the existing structure first (`ls {shared_dir}/notes/`), then save your note in the most appropriate location.

Examples:
- `notes/architecture/normalization/batch-vs-layer.md`
- `notes/optimization/learning-rate/warmup-findings.md`
- `notes/debugging/gradient-issues.md`

Create sub-folders as needed to keep related notes together. Update an existing note if it covers the same topic.

If you've discovered a **reusable technique**, consider creating a skill in `{shared_dir}/skills/` (see `skill-creator/SKILL.md`).

After reflecting, continue optimizing.
