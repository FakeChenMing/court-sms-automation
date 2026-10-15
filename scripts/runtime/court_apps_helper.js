function run(argv) {
  ObjC.import('Foundation');
  const listName = ObjC.unwrap($.NSProcessInfo.processInfo.environment.objectForKey('COURT_REMINDER_LIST')) || '法院材料';
  const mode = argv[0];
  if (mode === 'report-batch') {
    ObjC.import('Foundation');
    const data = $.NSString.stringWithContentsOfFileEncodingError(argv[1], $.NSUTF8StringEncoding, null);
    const items = JSON.parse(ObjC.unwrap(data));
    const app = Application('com.apple.reminders');
    const list = app.lists.byName(listName);
    const names = list.reminders.name();
    const indexByRowid = {};
    names.forEach((name, index) => {
      const match = name.match(/短信(\d+)$/);
      if (match) indexByRowid['sms-' + match[1]] = index;
    });
    const confirmed = [];
    for (const item of items) {
      const body = item.body + '\n\n处理记录标识：' + item.report_id;
      let reminder;
      const index = indexByRowid[item.report_id];
      if (index !== undefined) {
        reminder = list.reminders[index];
        reminder.body = body;
      } else {
        reminder = app.Reminder({name: item.title, body: body});
        list.reminders.push(reminder);
      }
      // Read back the identifier; a timed-out batch is safe to replay by SMS identity.
      if (!reminder.id()) throw new Error('Missing reminder identifier');
      confirmed.push(item.report_id);
    }
    return JSON.stringify({confirmed: confirmed});
  }

  if (mode === 'reminders') {
    const RemindersApp = Application('com.apple.reminders');
    const list = RemindersApp.lists.byName(listName);
    const caseNumber = argv[1];
    const existing = list.reminders.name();
    let added = 0;
    for (let i = 2; i < argv.length; i += 1) {
      const path = argv[i];
      const fileName = path.split('/').pop();
      const title = '法院新材料待处理 - ' + caseNumber + ' - ' + fileName;
      if (existing.indexOf(title) !== -1) continue;
      list.reminders.push(RemindersApp.Reminder({name: title, body: caseNumber + '\n' + path}));
      added += 1;
    }
    return 'added=' + added;
  }

  if (mode === 'report') {
    const RemindersApp = Application('com.apple.reminders');
    const list = RemindersApp.lists.byName(listName);
    const reportId = argv[1];
    const title = argv[2];
    const body = argv[3] || '';
    const marker = '处理记录标识：' + reportId;
    const fullBody = body + '\n\n' + marker;
    const existing = list.reminders.byName(title);
    if (existing.exists()) {
      existing.body = fullBody;
      return 'updated';
    }
    list.reminders.push(RemindersApp.Reminder({name: title, body: fullBody}));
    return 'added';
  }

  if (mode === 'manual') {
    const RemindersApp = Application('com.apple.reminders');
    const list = RemindersApp.lists.byName(listName);
    const title = argv[1];
    const body = argv[2] || '';
    if (list.reminders.name().indexOf(title) === -1) {
      list.reminders.push(RemindersApp.Reminder({name: title, body: body}));
    }
    return 'ok';
  }

  throw new Error('unsupported mode: ' + mode);
}
