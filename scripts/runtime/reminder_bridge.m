#import <Foundation/Foundation.h>
#import <EventKit/EventKit.h>
static BOOL Await(BOOL *done, NSTimeInterval timeout) {
 NSDate *end=[NSDate dateWithTimeIntervalSinceNow:timeout];
 while (!*done && [end timeIntervalSinceNow]>0) [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
 return *done;
}
int main(int argc,const char **argv) { @autoreleasepool {
 if(argc!=3) return 64;
 EKEventStore *store=[EKEventStore new];
 __block BOOL done=NO,allowed=NO;
 if([EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder]==EKAuthorizationStatusFullAccess) allowed=YES;
 else {
  [store requestFullAccessToRemindersWithCompletion:^(BOOL ok,NSError *err){allowed=ok;done=YES;}];
  if(!Await(&done,60)||!allowed){fprintf(stderr,"Reminders permission required\n");return 77;}
 }
 EKCalendar *list=nil;
 for(EKCalendar *c in [store calendarsForEntityType:EKEntityTypeReminder]) if([c.title isEqualToString:(NSProcessInfo.processInfo.environment[@"COURT_REMINDER_LIST"] ?: @"法院材料")]) {if(list)return 69;list=c;}
 if(!list||!list.allowsContentModifications) {fprintf(stderr,"Reminder list unavailable\n");return 69;}
 NSError *error=nil;
 NSArray *items=[NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:@(argv[1])] options:0 error:&error];
 if(![items isKindOfClass:NSArray.class]) return 65;
 done=NO;__block NSArray *existing=nil;
 [store fetchRemindersMatchingPredicate:[store predicateForRemindersInCalendars:@[list]] completion:^(NSArray *r){existing=r;done=YES;}];
 if(!Await(&done,45)||!existing) return 70;
 NSMutableDictionary *byId=[NSMutableDictionary dictionary];
 NSRegularExpression *rx=[NSRegularExpression regularExpressionWithPattern:@"短信([0-9]+)$" options:0 error:nil];
 for(EKReminder *r in existing){NSString *title=r.title?:@"";NSTextCheckingResult *match=[rx firstMatchInString:title options:0 range:NSMakeRange(0,title.length)];if(match){NSString *key=[@"sms-" stringByAppendingString:[title substringWithRange:[match rangeAtIndex:1]]];byId[key]=r;} NSString *notes=r.notes?:@""; NSRange marker=[notes rangeOfString:@"处理记录标识：" options:NSBackwardsSearch]; if(marker.location!=NSNotFound){NSString *key=[[notes substringFromIndex:NSMaxRange(marker)] componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet].firstObject; if(key.length)byId[key]=r;}}
 NSMutableArray *confirmed=[NSMutableArray array];NSMutableDictionary *receipts=[NSMutableDictionary dictionary];
 for(NSDictionary *item in items){
  NSString *key=item[@"report_id"];if(!key||!item[@"title"]||!item[@"body"])return 65;
  EKReminder *r=byId[key];if(!r){r=[EKReminder reminderWithEventStore:store];r.calendar=list;}
  r.title=item[@"title"];r.notes=[NSString stringWithFormat:@"%@\n\n处理记录标识：%@",item[@"body"],key];
  if(![store saveReminder:r commit:YES error:&error]){fprintf(stderr,"Reminder save failed\n");return 72;}
  EKReminder *saved=(EKReminder *)[store calendarItemWithIdentifier:r.calendarItemIdentifier];
  if(!saved||![saved.notes isEqualToString:r.notes])return 73;
  byId[key]=r;[confirmed addObject:key];receipts[key]=r.calendarItemIdentifier;
  // Durable progress after each write: recover partial batches without re-creating reminders.
  NSData *data=[NSJSONSerialization dataWithJSONObject:@{@"confirmed":confirmed,@"receipts":receipts} options:0 error:&error];
  if(![data writeToFile:@(argv[2]) options:NSDataWritingAtomic error:&error])return 74;
 }
 printf("confirmed=%lu\n",(unsigned long)confirmed.count);return 0;
}}
