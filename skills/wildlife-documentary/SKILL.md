# Wildlife Documentary Prompt and Video

Make a wildlife documentary episode in Thế giới động vật style with automuse-multi. The repo lives at `~/workspace/automuse-multi` and the CLI runs from its root with `python cli.py`.

## Plan the episode

- Build episodes of 1 to 3 minutes, 6 to 12 scenes, instead of one long film.

- A 31 minute film needs about 190 ten second clips, and the default weekly limit is 20 videos and 120 minutes per account, so plan a series of short episodes and let each episode cover one species or one survival mechanism.

- Pick one keystone species to carry the whole episode, and pick one thesis line that reverses intuition, for example that the cold is a material rather than an enemy.

- Write the script as YAML with `title`, `kind: documentary`, `aspect: '16:9'`, and scenes carrying `title`, `duration`, `visual`, and `narration`.

- Give every scene one survival mechanism, one species, and one concrete number, such as a temperature, a depth, a count of eggs, or a distance.

## Write the script

- Open with contrast and numbers, not general introduction.

- Put the harsh condition first, then the surprising number that contradicts it, then the small animal doing the impossible thing.

- Keep narration in Vietnamese, one idea per sentence, and keep sentences short enough to read aloud in one breath.

- Use the clips' own native audio as the background under the narration, and add nothing else. Do not add background music or an external ambience track — added music and ambience sound fake. The native audio is already matched to each scene (a chase scene is naturally louder and tenser than a dawn scene). Note the build pipeline strips the clips' audio when it assembles the final, so re-attach it afterwards from `output/<episode>/clips/*.mp4` (concatenate the 10 second audio tracks in scene order, then mix the narration on top at unity volume).

- End on the thesis line so the viewer remembers one sentence after the episode.

- Keep the example script at `examples/the-gioi-dong-vat.yaml` as the shape to copy, and keep new scripts under `scripts/`.

## Generate the video prompts

- Run `python cli.py prompts --script scripts/<name>.yaml` to print one English prompt per scene.

- Add `--account <name>` to have Muse write the prompts through the gateway, and add `--apply` to write the prompts into `visual` and save `<name>.prompts.yaml` for the build.

- Without `--account` the command uses the built-in template, which gets the key subjects right but reads awkwardly, so treat template prompts as a draft and Muse prompts as the polished version.

- A prompt written through `--account` travels through the account main chat session, so the request appears in that chat history, exactly like a video generation request.

- Check that every prompt names the animal and the action, names a camera framing, uses documentary cinematography language, and carries no dialogue text, since the video is silent apart from narration.

## Build the video

- Run `python cli.py build --script scripts/<name>.prompts.yaml` to generate the clips, dub the narration, and stitch the final video.

- A build needs a Muse account cookie and an ElevenLabs key, and it consumes weekly quota, so run `python cli.py build --script <file> --mock` first when testing the pipeline without spending quota.

- Set the narration voice with `python cli.py voices set-default <name-or-id>`, and set a clear delivery with `python cli.py voices set-stability 0.7` and `python cli.py voices set-speed 1.0`.

- If a scene fails to generate, rebuild it in a gentler way without asking again: drop the element that caused the failure and keep the story beat. For example, a scene of lions feeding on prey failed to generate, so it was replaced with the pride resting together in the shade while the lioness grooms her cub, and the narration was rewritten to match (lions sleep up to 20 hours a day to recover).

- Keep the generated clips at 1280x720 even when the script asks for 8 seconds, because the video model returns 10 second clips, and plan scene durations around that.

- Verify the finished file before delivering it, checking exact duration, 16:9 aspect, and that narration is present and clear.

## Push and keep the repo straight

- Push only when the user says so, and use the snapshot push-files flow to `tunhary754-maker/automuse-multi` on branch `main`.

- Never run a plain `git push`, because the local remote still points at the stale `ducpro283` copy.

- Read the sync record at `~/workspace/automuse-multi.muse-github-snapshot-sync.json` before pushing, verify the remote head still matches it, and append the new entry after a confirmed push.
