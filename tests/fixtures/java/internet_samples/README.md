# Internet-derived Java stack trace samples

These fixtures are intentionally shortened and, where appropriate, anonymized or
adapted. They preserve stack-trace structures seen in public examples without
copying complete third-party logs.

- `null_pointer.txt`: Oracle `Throwable.printStackTrace()` example.
  https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/lang/Throwable.html
- `stack_overflow_spring.txt`: Spring bean creation ending in recursive
  `StackOverflowError`, adapted from a public Stack Overflow example.
  https://stackoverflow.com/questions/62396691/
- `bean_creation_no_such_method.txt`: Spring bean creation ending in
  `NoSuchMethodError`, adapted from an Atlassian Bamboo support example.
  https://support.atlassian.com/bamboo/kb/remote-agent-fails-to-start-up-error-creating-bean-with-name-jmsmessageconvertertarget/
- `out_of_memory.txt`: `OutOfMemoryError: Java heap space` shape based on a public
  example and the JDK API definition.
  https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/lang/OutOfMemoryError.html
- `sql_constraint.txt`: JDBC exception form based on Oracle's SQLException
  documentation; application and vendor frames are synthetic.
  https://docs.oracle.com/javase/tutorial/jdbc/basics/sqlexception.html
- `class_not_found.txt`: class-loading failure based on the JDK API definition;
  stack frames are representative.
  https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/lang/ClassNotFoundException.html
- `completion_exception.txt`: async wrapper/cause form based on the JDK
  `CompletionException` contract and standard Java cause chaining.
  https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/util/concurrent/CompletionException.html
