from datetime import datetime, timezone


# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"


class Error_Log_system ():
    def __init__(self):
        pass

    def catching_errors(self,error_var,section, ats_vendor, slug_name = None):
        print(f'{ANSI_RED} {error_var} {ANSI_RESET}')
        time_of_error = datetime.now().astimezone().isoformat(timespec="seconds")

        error_log = f"{time_of_error}\nthere was an error at {section}\nThis is the error code: {error_var}\nIt came from this ATS VENDOR: {ats_vendor}"
        
        if slug_name != None:
            slug_error = f'\nThe error is related to this company name: {slug_name}' 
            error_log += slug_error

        self.logging_errors(error_log)

    def logging_errors(self, error_log):

        new_section = error_log

        with open("errors.txt", "a", encoding="utf-8") as file:
            file.write("\n\n\n" + new_section)

    def icims_error_catching(self,error_var,job_url):
        time_of_error = datetime.now().astimezone().isoformat(timespec="seconds")
        error_log = f'{time_of_error}\nwe caught a description error with ICIMS\nthis is the error code: {error_var}\n this is the job causing the problem{job_url}'


