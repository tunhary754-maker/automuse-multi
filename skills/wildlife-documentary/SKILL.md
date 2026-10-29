# Wildlife Documentary Prompt and Video

Make a wildlife documentary episode in Thế giới động vật style with automuse-multi. The repo lives at `~/workspace/automuse-multi` and the CLI runs from its root with `python cli.py`.

## Plan the episode

- Build episodes of 1 to 3 minutes, 6 to 12 scenes, instead of one long film.

- A 31 minute film needs about 190 ten second clips, so plan a series of short episodes and let each episode cover one species or one survival mechanism. The local weekly tracker no longer caps builds, but the real Muse account limit is unknown (there is no public API for it), so short episodes are still the safe shape.

- Pick one keystone species to carry the whole episode, and pick one thesis line that reverses intuition, for example that the cold is a material rather than an enemy.

- Write the script as YAML with `title`, `kind: documentary`, `aspect: '16:9'`, and scenes carrying `title`, `duration`, `visual`, and `narration`.

- Give every scene one survival mechanism, one species, and one concrete number, such as a temperature, a depth, a count of eggs, or a distance.

## Create the script

- If you need topic ideas first, run `python cli.py topics --kind documentary` to list suggestions. Add `--keyword <word>` to filter and `--count <n>` to change how many are shown. Add `--account <name>` to have Muse suggest topics through the gateway; without `--account` the command prints a built-in curated list.

- To have Muse write the script from a topic, run `python cli.py write-script --topic "<topic>" --kind documentary --scenes <n> --account <name>`. The script is saved to `scripts/<slug>.yaml` (the topic slugified with Vietnamese diacritics stripped, for example "Cá Voi Xanh" becomes `scripts/ca-voi-xanh.yaml`); add `--out <path>` to save elsewhere and `--duration <seconds>` to change the per-scene length (default 10).

- A `write-script` request with `--account` travels through the account main chat session, so the request appears in that chat history, exactly like a video generation request.

- Without `--account`, `write-script` saves a template skeleton instead of a Muse-written script: the scenes carry English visual placeholders and narration placeholders like `[Viết lời dẫn cho cảnh N về …]` that must be filled in before building. Treat the skeleton as a draft, not a finished script.

- After `write-script`, always read the saved YAML and check that every scene has a real `visual` and a real Vietnamese `narration` following the rules in the next section, before moving on to prompts and build.

## Write the script

- Open with contrast and numbers, not general introduction.

- Put the harsh condition first, then the surprising number that contradicts it, then the small animal doing the impossible thing.

- Keep narration in Vietnamese, one idea per sentence, and keep sentences short enough to read aloud in one breath.

- End on the thesis line so the viewer remembers one sentence after the episode.

- Keep the example script at `examples/the-gioi-dong-vat.yaml` as the shape to copy, and keep new scripts under `scripts/`.

## Export the narration to a separate txt file

- The narration is NOT dubbed in the pipeline. No ElevenLabs key is needed, no voice is set, and no dubbing step runs. The user dubs the narration themselves afterwards, from the txt file.

- After the script is written, export its narration to a separate txt file next to the script, one block per scene in order, with scene id, title, timecode, and the narration line. The user reads this file when dubbing.

- Run this from the repo root (replace `<name>` with the script name):

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from automuse.script import load_script
s = load_script('scripts/<name>.yaml')
t = 0
out = []
for sc in s.scenes:
    d = sc.duration or 10
    out.append(f\"{sc.id} — {sc.title} ({t//60:02d}:{t%60:02d}–{(t+d)//60:02d}:{(t+d)%60:02d})\")
    out.append(sc.narration or '')
    out.append('')
    t += d
open('scripts/<name>-loi-dan.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('Đã lưu scripts/<name>-loi-dan.txt')
"
```

- The txt file is the dubbing script. It travels with the episode, but it is never fed to the video build.

## Generate the video prompts

- Run `python cli.py prompts --script scripts/<name>.yaml` to print one English prompt per scene.

- Add `--account <name>` to have Muse write the prompts through the gateway, and add `--apply` to write the prompts into `visual` and save `<name>.prompts.yaml` for the build.

- Without `--account` the command uses the built-in template, which gets the key subjects right but reads awkwardly, so treat template prompts as a draft and Muse prompts as the polished version.

- A prompt written through `--account` travels through the account main chat session, so the request appears in that chat history, exactly like a video generation request.

- Check that every prompt names the animal and the action, names a camera framing, uses documentary cinematography language, and carries no dialogue text, since the video is silent apart from the narration the user dubs later.

## Build the video

- Run `python cli.py build --script scripts/<name>.prompts.yaml --no-tts` to generate the clips and stitch the final video. The build needs only a Muse account cookie — no ElevenLabs key, no voice setup, no dubbing step. The video script is built exactly as written.

- A build consumes real account quota (the exact limit is unknown, there is no public API for it), so run `python cli.py build --script <file> --mock` first when testing the pipeline without spending quota.

- If a scene fails to generate, rebuild it in a gentler way without asking again: drop the element that caused the failure and keep the story beat. For example, a scene of lions feeding on prey failed to generate, so it was replaced with the pride resting together in the shade while the lioness grooms her cub, and the narration was rewritten to match (lions sleep up to 20 hours a day to recover).

- Keep the generated clips at 1280x720 even when the script asks for 8 seconds, because the video model returns 10 second clips, and plan scene durations around that.

- The build pipeline strips the clips' native audio when it assembles the final, so re-attach it afterwards from `output/<episode>/clips/*.mp4`: concatenate the audio tracks in scene order and mux them onto the final. Use the clips' own native audio as the background — add nothing else. Do not add background music or an external ambience track — added music and ambience sound fake. The native audio is already matched to each scene (a chase scene is naturally louder and tenser than a dawn scene).

- Native audio levels vary a lot from clip to clip, so ALWAYS check the volume after muxing with `ffmpeg -i final.mp4 -af volumedetect -vn -sn -dn -f null /dev/null`. If the mean volume is around -20 to -27 dB, it is audible as-is (penguin and blue whale demos measured -22.7 and -22.5 dB). If the mean is below -30 dB, the track exists but sounds like silence — normalize it before delivering with `ffmpeg -y -i final.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy -c:a aac -b:a 192k final-norm.mp4` and replace final.mp4 with the normalized file. Example: the elephant demo measured -36.6 dB mean after muxing and was normalized to -17.1 dB mean (max -1.3 dB, no clipping). Never deliver a final whose audio measures below -30 dB mean.

- Verify the finished file before delivering it, checking exact duration, 16:9 aspect, and that the native audio is present AND audible (mean volume -17 to -27 dB, max below 0 dB). A track that exists but measures below -30 dB mean is not audible — normalize it, do not deliver it quiet. There is no narration track in the delivered file — the user dubs from the txt file afterwards.

## Push and keep the repo straight

- Push only when the user says so, and use the snapshot push-files flow to `tunhary754-maker/automuse-multi` on branch `main`.

- Never run a plain `git push`, because the local remote still points at the stale `ducpro283` copy.

- Read the sync record at `~/workspace/automuse-multi.muse-github-snapshot-sync.json` before pushing, verify the remote head still matches it, and append the new entry after a confirmed push.
