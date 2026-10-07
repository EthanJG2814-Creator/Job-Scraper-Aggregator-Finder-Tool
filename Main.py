from The_Database.db_connect import database_connection_system
import os

# color code
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_ORANGE = "\033[38;5;208m"

class main():
    def __init__(self):
        self.database = database_connection_system()

    def create_database(self):
        self.database.create_database_table()

    # def delete_database(self):
    #     file_path = "The_Database/job_data.db"

    #     try:
    #         self.database.close_database()
    #         os.remove(file_path)
    #         print(f"{ANSI_GREEN}File deleted successfully.{ANSI_RESET}")
    #     except FileNotFoundError:
    #         print(f"{ANSI_RED}The file does not exist.{ANSI_RESET}")
    #     except PermissionError:
    #         print(f"{ANSI_RED}Permission denied.{ANSI_RESET}")
    #     except Exception as e:
    #         print(f"{ANSI_RED}An error occurred: {e}{ANSI_RESET}")


if __name__ == "__main__":
    print("Hello, welcome to the main python execution file \n   " \
    "What command would you like to run today? \n")

    print("-"*20)
    main = main()

    while True:
        print("here is a list of your command options, just type a number for which one you want to execute:")
        print("1. Run Main")
        print("2. Create job_data.db with Tables")
        print("3. Delete job_data.db with Tables")

        user_input = input("Action: ")

        if user_input == "2":
            main.create_database()

        # elif user_input == "3":
        #     main.delete_database()

        else:
            print(f'{ANSI_RED} That is not a valid input please try again{ANSI_RESET}')


        

'''
to run this file run this command:
pythin -m Main


'''


