# TimeLapse

TimeLapse turns recorded UniFi Protect footage into export artifacts and can repeat that work for each completed calendar day.

## Language

**Daily Automation**:
A named, durable intent to export every completed calendar day for a chosen set of cameras according to one captured timezone.
_Avoid_: Daily schedule, recurring job

**Export Batch**:
All Export Jobs needed by one Daily Automation for one calendar day.
_Avoid_: Daily job, group

**Export Job**:
The export of one camera over one time range into one Export Artifact.
_Avoid_: Download, task

**Export Artifact**:
The validated MP4 produced by an Export Job.
_Avoid_: Output file, video file

**Processed Day**:
A calendar day whose Export Batch completed successfully for a Daily Automation. A stopped automation may finish and process its current day, and deleting one of that day's Export Artifacts does not make the day unprocessed.
_Avoid_: Completed date, last run
