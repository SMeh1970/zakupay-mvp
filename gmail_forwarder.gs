/**
 * Google Apps Script bridge: Gmail -> Zakupay MVP.
 *
 * Put the two secrets in Script Properties as WEBHOOK_URL and WEBHOOK_SECRET,
 * then create a time trigger for forwardZakupayEmails every 5 minutes.
 */
function forwardZakupayEmails() {
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('WEBHOOK_URL');
  const secret = props.getProperty('WEBHOOK_SECRET');
  if (!url || !secret) throw new Error('WEBHOOK_URL/WEBHOOK_SECRET are not configured');

  const labelName = 'ZakupayProcessed';
  const label = GmailApp.getUserLabelByName(labelName) || GmailApp.createLabel(labelName);
  const threads = GmailApp.search('from:(sel-be.ru) newer_than:7d -label:' + labelName, 0, 100);

  threads.forEach(function(thread) {
    let ok = true;
    thread.getMessages().forEach(function(message) {
      if (!message.isInInbox()) return;
      const response = UrlFetchApp.fetch(url, {
        method: 'post',
        contentType: 'message/rfc822',
        payload: message.getRawContent(),
        headers: {'X-Webhook-Secret': secret},
        muteHttpExceptions: true
      });
      const code = response.getResponseCode();
      if (code < 200 || code >= 300) {
        ok = false;
        console.error('Zakupay forwarding failed: HTTP ' + code + ' ' + response.getContentText());
      }
    });
    if (ok) thread.addLabel(label);
  });
}
