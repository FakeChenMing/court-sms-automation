#import <Foundation/Foundation.h>
#import <EventKit/EventKit.h>
int main(int argc,const char **argv){@autoreleasepool{
 if(argc!=9 || strcmp(argv[1],"upsert"))return 64;
 EKEventStore *store=[EKEventStore new]; __block BOOL done=NO,allowed=NO;
 if([EKEventStore authorizationStatusForEntityType:EKEntityTypeEvent]==EKAuthorizationStatusFullAccess)allowed=YES;
 else{[store requestFullAccessToEventsWithCompletion:^(BOOL ok,NSError *e){allowed=ok;done=YES;}];NSDate *end=[NSDate dateWithTimeIntervalSinceNow:60];while(!done&&end.timeIntervalSinceNow>0)[[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];}
 if(!allowed)return 77;
 EKCalendar *calendar=nil;for(EKCalendar *c in [store calendarsForEntityType:EKEntityTypeEvent])if([c.title isEqualToString:@(argv[2])]){if(calendar)return 69;calendar=c;}
 if(!calendar||!calendar.allowsContentModifications)return 69;
 NSDate *start=[NSDate dateWithTimeIntervalSince1970:atof(argv[4])],*end=[NSDate dateWithTimeIntervalSince1970:atof(argv[5])];
 NSString *title=@(argv[3]),*notes=@(argv[7]),*marker=[notes componentsSeparatedByString:@"\n"].firstObject;
 NSURL *file=[NSURL fileURLWithPath:@(argv[8])];if(![[NSFileManager defaultManager] fileExistsAtPath:file.path])return 66;
 NSPredicate *predicate=[store predicateForEventsWithStartDate:[start dateByAddingTimeInterval:-60] endDate:[start dateByAddingTimeInterval:60] calendars:@[calendar]];
 EKEvent *event=nil;
 for(EKEvent *e in [store eventsMatchingPredicate:predicate])if([e.title isEqualToString:title]&&marker.length&&[e.notes containsString:marker]&&[e.notes containsString:@"[CourtSMSAutomation]"]&&fabs([e.startDate timeIntervalSinceDate:start])<1){if(event)return 73;event=e;}
 if(!event){event=[EKEvent eventWithEventStore:store];event.calendar=calendar;}
 NSString *link=file.absoluteString;NSString *previous=event.notes?:@"";
 NSString *merged=[notes stringByAppendingString:@"\n[CourtSMSAutomation]"];
 for(NSString *line in [previous componentsSeparatedByString:@"\n"])if([line hasPrefix:@"file://"]&&![merged containsString:line])merged=[merged stringByAppendingFormat:@"\n%@",line];
 if(![merged containsString:link])merged=[merged stringByAppendingFormat:@"\n%@",link];
 event.title=title;event.startDate=start;event.endDate=end;event.location=@(argv[6]);event.notes=merged;event.URL=file;
 NSError *error=nil;if(![store saveEvent:event span:EKSpanThisEvent commit:YES error:&error])return 72;
 EKEvent *saved=[store eventWithIdentifier:event.eventIdentifier];if(!saved||fabs([saved.startDate timeIntervalSinceDate:start])>=1||![saved.notes containsString:link])return 74;
 puts("verified");return 0;
}}
