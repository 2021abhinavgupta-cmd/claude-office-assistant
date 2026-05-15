const fs = require('fs');
let c = fs.readFileSync('frontend/projects.html', 'utf8');

// Find the exact broken section and replace the whole kavatar div
const broken = `        <div class="kavatar" style="background:\${color}" title="\${assigneeName}"
          onclick="event.stopPropagation();openAssigneePicker(event,'\${taskId}','\${notionId}',\${t.isQuick?'true':'false'},'</`;

const idx = c.indexOf('<div class="kavatar" style="background:${color}" title="${assigneeName}"\n          onclick="event.stopPropagation();openAssigneePicker(event');

console.log('Found at:', idx);

if (idx > -1) {
  // Find the end of this div closing tag
  const endMarker = '>${initial}</div>';
  const endIdx = c.indexOf(endMarker, idx);
  console.log('End at:', endIdx);

  if (endIdx > -1) {
    const before = c.slice(0, idx);
    const after  = c.slice(endIdx + endMarker.length);
    const newDiv = `<div class="kavatar" style="background:\${color}" title="\${assigneeName}"
          onclick="event.stopPropagation();openAssigneePicker(this)"
        >\${initial}</div>`;
    fs.writeFileSync('frontend/projects.html', before + newDiv + after, 'utf8');
    console.log('FIXED successfully!');
  }
} else {
  console.log('Could not find start marker');
}
